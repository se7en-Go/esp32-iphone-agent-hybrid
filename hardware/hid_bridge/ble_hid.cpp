// BLE HID mouse + keyboard + consumer (AC Home, globe) beside the app GATT service.
// iOS requires bonding. Just Works is already the board's security mode (ble_begin).
// Reports wait until the host accepts the notify. NimBLE's NOTIFY_TX means "queued",
// not "on air", so the gesture also sleeps hid_pace_profile.gap_ms and asks for a
// 7.5–15 ms connection interval only while the gesture runs.
#include "ble_hid.h"

#include <Preferences.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include "nimble/nimble/host/include/host/ble_hs.h"
#include "src/ble/pair_window.h"
#include "src/hid/transport.h"
#include "ble_link.h"
#include "logbuf.h"

static const uint8_t REPORT_MAP[] = {
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x01, 0x09, 0x01, 0xA1, 0x00, 0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15,
    0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02, 0x95, 0x01, 0x75, 0x05, 0x81, 0x03, 0x05, 0x01, 0x09, 0x30,
    0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06, 0x05, 0x0C, 0x0A, 0x38, 0x02,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06, 0xC0, 0xC0, 0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85,
    0x02, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01,
    0x75, 0x08, 0x81, 0x01, 0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75,
    0x03, 0x91, 0x03, 0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
    0xC0, 0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x03, 0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03,
    0x75, 0x10, 0x95, 0x01, 0x81, 0x00, 0xC0,
};

struct Bond {
  uint8_t type;
  uint8_t val[6];
};

static Preferences store;
static Bond bonds[8];
static int bondCount;
static int pref = HID_PREF_AUTO;
static bool pairArmed;
static uint32_t pairUntil;
static NimBLECharacteristic *mouseIn, *keyIn, *consIn;
static bool mouseOn, keyOn, consOn, haveConn;
static uint16_t connHandle;
static uint8_t buttons;
static uint8_t lastMod, lastKeys[6];
static uint16_t lastConsumer;

static bool encrypted(uint16_t h) {
  ble_gap_conn_desc d;
  return haveConn && h == connHandle && ble_gap_conn_find(h, &d) == 0 && d.sec_state.encrypted;
}

bool ble_hid_connected() { return (mouseOn || keyOn || consOn) && encrypted(connHandle); }
bool ble_hid_bonded() { return bondCount > 0; }
int ble_hid_pref() { return pref; }

static void savePref() { store.putUChar("mode", (uint8_t)pref); }

static void saveBonds() {
  store.putUChar("nb", (uint8_t)bondCount);
  store.putBytes("bonds", bonds, sizeof bonds);
}

void ble_hid_set_pref(int next) {
  if (next < HID_PREF_USB || next > HID_PREF_AUTO) return;
  pref = next;
  savePref();
}

bool ble_hid_pair_window_open() {
  int still = 0;
  int open = pair_window_still_open(pairArmed ? 1 : 0, pairUntil, millis(), &still);
  pairArmed = still != 0;
  return open != 0;
}

void ble_hid_allow_pairing(uint32_t window_ms) {
  if (window_ms < 5000) window_ms = 5000;
  if (window_ms > 120000) window_ms = 120000;
  pairArmed = true;
  pairUntil = millis() + window_ms;
}

void ble_hid_deny_pairing() { pairArmed = false; }

void ble_hid_remember(uint8_t type, const uint8_t addr[6]) {
  for (int i = 0; i < bondCount; i++)
    if (bonds[i].type == type && memcmp(bonds[i].val, addr, 6) == 0) return;
  if (bondCount >= 8) return;
  bonds[bondCount].type = type;
  memcpy(bonds[bondCount].val, addr, 6);
  bondCount++;
  saveBonds();
}

static void clearLatch() {
  buttons = 0;
  lastMod = 0;
  memset(lastKeys, 0, sizeof lastKeys);
  lastConsumer = 0;
}

void ble_hid_bonds_cleared() {
  bondCount = 0;
  saveBonds();
  mouseOn = keyOn = consOn = haveConn = false;
  clearLatch();
}

void ble_hid_unpair() {
  for (int i = 0; i < bondCount; i++) {
    NimBLEAddress addr(bonds[i].val, bonds[i].type);
    NimBLEDevice::deleteBond(addr);
    ble_forget_addr(bonds[i].type, bonds[i].val);
    for (uint16_t h = 0; h < 8; h++) {
      ble_gap_conn_desc d;
      if (ble_gap_conn_find(h, &d) != 0) continue;
      if (d.peer_id_addr.type == bonds[i].type && memcmp(d.peer_id_addr.val, bonds[i].val, 6) == 0) {
        NimBLEServer *server = NimBLEDevice::getServer();
        if (server) server->disconnect(h);
      }
    }
  }
  ble_hid_bonds_cleared();
}

void ble_hid_on_disconnect(uint16_t handle) {
  if (!haveConn || handle != connHandle) return;
  mouseOn = keyOn = consOn = haveConn = false;
  clearLatch();
}

class HidCb : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &ci, uint16_t sub) override {
    bool on = sub & 1;
    if (c == mouseIn) mouseOn = on;
    else if (c == keyIn) keyOn = on;
    else if (c == consIn) consOn = on;
    else return;
    if (on) {
      haveConn = true;
      connHandle = ci.getConnHandle();
    } else if (!mouseOn && !keyOn && !consOn) {
      haveConn = false;
    }
  }
};

static HidCb hidCb;

static bool emit(NimBLECharacteristic *chr, const uint8_t *data, size_t n) {
  if (!chr || !ble_hid_connected()) return false;
  chr->setValue(data, n);
  uint32_t start = millis();
  while ((int32_t)(millis() - start) < 40) {
    if (!ble_hid_connected()) return false;
    // Leave the video reserve alone: one HID report is a single mbuf.
    if (os_msys_num_free() >= 4) {
      os_mbuf *om = ble_hs_mbuf_from_flat(data, n);
      int rc = om ? ble_gatts_notify_custom(connHandle, chr->getHandle(), om) : BLE_HS_ENOMEM;
      if (rc == 0) return true;
      if (rc != BLE_HS_ENOMEM) return false;
    }
    delay(2);
  }
  return false;
}

bool ble_hid_mouse(int8_t x, int8_t y, int8_t wheel, int8_t pan) {
  uint8_t report[5] = {buttons, (uint8_t)x, (uint8_t)y, (uint8_t)wheel, (uint8_t)pan};
  return emit(mouseIn, report, sizeof report);
}

bool ble_hid_button(bool left_down) {
  uint8_t next = left_down ? 0x01 : 0x00;
  if (next == buttons) return true;
  uint8_t prev = buttons;
  buttons = next;
  if (ble_hid_mouse(0, 0, 0, 0)) return true;
  buttons = prev;
  return false;
}

bool ble_hid_keyboard(uint8_t modifier, const uint8_t *keys, int nkeys) {
  uint8_t report[8] = {modifier, 0};
  int n = nkeys < 0 ? 0 : nkeys;
  if (n > 6) n = 6;
  if (keys)
    for (int i = 0; i < n; i++) report[2 + i] = keys[i];
  if (modifier == lastMod && memcmp(report + 2, lastKeys, 6) == 0) return true;
  if (!emit(keyIn, report, sizeof report)) return false;
  lastMod = modifier;
  memcpy(lastKeys, report + 2, 6);
  return true;
}

bool ble_hid_consumer(uint16_t usage) {
  if (usage == lastConsumer) return true;
  if (!emit(consIn, (const uint8_t *)&usage, sizeof usage)) return false;
  lastConsumer = usage;
  return true;
}

void ble_hid_release_all() {
  ble_hid_button(false);
  ble_hid_keyboard(0, NULL, 0);
  ble_hid_consumer(0);
}

void ble_hid_arm_interval(int tight) {
  if (!ble_hid_connected()) return;
  NimBLEServer *server = NimBLEDevice::getServer();
  if (!server) return;
  if (tight) server->updateConnParams(connHandle, 6, 12, 0, 400);
  else server->updateConnParams(connHandle, 12, 24, 0, 400);
}

String ble_hid_command(const char *arg) {
  hid_cmd_t cmd;
  if (hid_parse_cmd(arg, &cmd) != 0) return "err usage: hid usb|ble|auto|pair|unpair";
  if (cmd.kind == HID_CMD_QUERY) return String("ok ") + hid_pref_name(pref);
  if (cmd.kind == HID_CMD_SET) {
    ble_hid_set_pref(cmd.pref);
    return "ok";
  }
  if (cmd.kind == HID_CMD_PAIR) {
    ble_hid_allow_pairing(60000);
    return "ok";
  }
  if (cmd.kind == HID_CMD_PAIR_OFF) {
    ble_hid_deny_pairing();
    return "ok";
  }
  if (cmd.kind == HID_CMD_UNPAIR) {
    ble_hid_unpair();
    return "ok";
  }
  return "err usage: hid usb|ble|auto|pair|unpair";
}

void ble_hid_begin(NimBLEServer *server) {
  store.begin("hid", false);
  uint8_t saved = store.getUChar("mode", HID_PREF_AUTO);
  pref = saved > HID_PREF_AUTO ? HID_PREF_AUTO : saved;
  bondCount = store.getUChar("nb", 0);
  if (bondCount > 8) bondCount = 0;
  if (bondCount) store.getBytes("bonds", bonds, sizeof bonds);

  auto *hid = new NimBLEHIDDevice(server);
  hid->setManufacturer("ESP32 Agent");
  hid->setPnp(0x02, 0x303A, 0x81B2, 0x0100);
  hid->setHidInfo(0x00, 0x02);
  hid->setBatteryLevel(100);
  hid->setReportMap(const_cast<uint8_t *>(REPORT_MAP), sizeof REPORT_MAP);
  uint8_t reportMode = 0x01;
  hid->getProtocolMode()->setValue(&reportMode, 1);
  mouseIn = hid->getInputReport(1);
  keyIn = hid->getInputReport(2);
  consIn = hid->getInputReport(3);
  hid->getOutputReport(2);
  mouseIn->setCallbacks(&hidCb);
  keyIn->setCallbacks(&hidCb);
  consIn->setCallbacks(&hidCb);
  board_log("ble hid: mode %s, bonds %d\n", hid_pref_name(pref), bondCount);
}
