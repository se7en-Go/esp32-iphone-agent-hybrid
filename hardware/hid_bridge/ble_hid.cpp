// BLE HID mouse + keyboard + consumer (AC Home, globe) beside the app GATT service.
// iOS requires bonding. Just Works is already the board's security mode (ble_begin).
// Reports wait until the host accepts the notify, leaving a 16-block mbuf reserve.
// Mbuf availability is a preflight check, not a per-report completion signal: the
// ESP32 VHCI transport can free a report's mbuf before notify returns. The connection
// interval stays at the 15 ms tuneLink already set: a shorter request is outside
// Apple's rules, and a longer one would slow GATT video on the same ACL.
#include "ble_hid.h"

#include <Preferences.h>
#include <atomic>
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
static std::atomic<bool> unpairing;
static std::atomic<uint32_t> sessionEpoch;

static int blocksFor(size_t n) {
  const int data = MYNEWT_VAL(MSYS_1_BLOCK_SIZE) - 16;
  return (int)((n + 3 + 4 + 4 + 8 + data - 1) / data) + 1;
}

static bool encrypted(uint16_t h) {
  ble_gap_conn_desc d;
  return haveConn && h == connHandle && ble_gap_conn_find(h, &d) == 0 && d.sec_state.encrypted;
}

bool ble_hid_connected() { return (mouseOn || keyOn || consOn) && encrypted(connHandle); }
bool ble_hid_session_active() {
  ble_gap_conn_desc d;
  return haveConn && ble_gap_conn_find(connHandle, &d) == 0;
}
bool ble_hid_bonded() { return bondCount > 0; }
int ble_hid_pref() { return pref; }

static void savePref() { store.putUChar("mode", (uint8_t)pref); }

static void saveBonds() {
  store.putUChar("nb", (uint8_t)bondCount);
  store.putBytes("bonds", bonds, sizeof bonds);
}

bool ble_hid_set_pref(int next) {
  if (next < HID_PREF_USB || next > HID_PREF_AUTO) return false;
  if (next == pref) return true;
  if (!hid_prepare_pref(next)) return false;
  pref = next;
  savePref();
  return true;
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
  // A phone that already bonded through the companion window is encrypted now.
  // Copy it into the HID list so a later unpair can delete that shared bond.
  for (uint16_t h = 0; h < 8; h++) {
    ble_gap_conn_desc d;
    if (ble_gap_conn_find(h, &d) != 0 || !d.sec_state.encrypted) continue;
    ble_hid_remember(d.peer_id_addr.type, d.peer_id_addr.val);
  }
}

void ble_hid_deny_pairing() { pairArmed = false; }

void ble_hid_remember(uint8_t type, const uint8_t addr[6]) {
  if (unpairing) return; // a disconnect/subscription callback must not reinsert a deleted bond
  for (int i = 0; i < bondCount; i++)
    if (bonds[i].type == type && memcmp(bonds[i].val, addr, 6) == 0) return;
  if (bondCount >= 8) return;
  bonds[bondCount].type = type;
  memcpy(bonds[bondCount].val, addr, 6);
  bondCount++;
  saveBonds();
}

static void clearLatch() {
  // Invalidate reports waiting for mbufs, even if NimBLE reuses the ACL handle.
  sessionEpoch++;
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

bool ble_hid_using(uint16_t handle) {
  return haveConn && handle == connHandle && (mouseOn || keyOn || consOn);
}

static bool bondStored(const Bond &bond, bool &present) {
  ble_store_key_sec key = {};
  key.peer_addr.type = bond.type;
  memcpy(key.peer_addr.val, bond.val, sizeof bond.val);
  ble_store_value_sec value;
  // isBonded() hides read errors and only enumerates OUR_SEC. A partial delete
  // can leave PEER_SEC behind, so confirm both records and distinguish ENOENT.
  int ours = ble_store_read_our_sec(&key, &value);
  int peer = ble_store_read_peer_sec(&key, &value);
  if ((ours != 0 && ours != BLE_HS_ENOENT) || (peer != 0 && peer != BLE_HS_ENOENT)) return false;
  present = ours == 0 || peer == 0;
  return true;
}

bool ble_hid_unpair() {
  if (unpairing.exchange(true)) return false;
  if (!ble_pause_advertising()) {
    unpairing = false;
    return false;
  }
  bool ok = true;
  for (int i = 0; i < bondCount;) {
    NimBLEAddress addr(bonds[i].val, bonds[i].type);
    // NimBLE needs advertising stopped to remove an IRK. Check storage too:
    // some lower-level store errors are logged without reaching deleteBond's result.
    // A record already absent from NimBLE is safe to remove from our mirror.
    bool present;
    bool deleted = bondStored(bonds[i], present) &&
                   (!present || (NimBLEDevice::deleteBond(addr) && bondStored(bonds[i], present) && !present));
    if (!deleted) {
      ok = false;
      i++;
      continue;
    }
    ble_forget_addr(bonds[i].type, bonds[i].val);
    for (uint16_t h = 0; h < 8; h++) {
      ble_gap_conn_desc d;
      if (ble_gap_conn_find(h, &d) != 0) continue;
      if (d.peer_id_addr.type == bonds[i].type && memcmp(d.peer_id_addr.val, bonds[i].val, 6) == 0) {
        NimBLEServer *server = NimBLEDevice::getServer();
        if (server) server->disconnect(h);
      }
    }
    memmove(bonds + i, bonds + i + 1, (bondCount - i - 1) * sizeof(Bond));
    bondCount--;
    saveBonds();
  }
  if (!bondCount) ble_hid_bonds_cleared();
  ble_resume_advertising();
  unpairing = false;
  return ok;
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
      // Remember any encrypted central that subscribes, not only one that
      // bonded while the HID window was open. Otherwise hid unpair misses it.
      if (ci.isEncrypted()) {
        NimBLEAddress id = ci.getIdAddress();
        ble_hid_remember(id.getType(), id.getVal());
      }
    }
    // Losing CCCD subscriptions is not a disconnect. Keep the owner and latch
    // until onDisconnect so a temporarily unavailable release stays retryable.
  }
};

static HidCb hidCb;

static bool emit(NimBLECharacteristic *chr, const uint8_t *data, size_t n) {
  uint32_t epoch = sessionEpoch;
  uint16_t handle = connHandle;
  if (!chr || !ble_hid_connected()) return false;
  chr->setValue(data, n);
  const int reserve = 16; // same MBUF_RESERVE video refuses to enter
  int blocks = blocksFor(n);
  uint32_t start = millis();
  while ((int32_t)(millis() - start) < 40) {
    if (epoch != sessionEpoch || !ble_hid_connected()) return false;
    if (os_msys_num_free() >= blocks + reserve) {
      os_mbuf *om = ble_hs_mbuf_from_flat(data, n);
      int rc = om ? ble_gatts_notify_custom(handle, chr->getHandle(), om) : BLE_HS_ENOMEM;
      if (rc == 0) return epoch == sessionEpoch; // accepted by this session, not an air acknowledgement
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
  uint8_t report[5] = {next, 0, 0, 0, 0};
  // Commit only accepted state. A disconnect while emit waits clears the latch;
  // rolling back a failed release would resurrect that ended session's button.
  if (!emit(mouseIn, report, sizeof report)) return false;
  buttons = next;
  return true;
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

void ble_hid_arm_interval(int) {
  // Intentionally empty. tuneLink already asked for 15/15 ms. Apple rejects a
  // 7.5 ms minimum, and opening the range to 30 ms after a gesture slows the
  // GATT video that shares this ACL.
}

String ble_hid_command(const char *arg) {
  hid_cmd_t cmd;
  if (hid_parse_cmd(arg, &cmd) != 0) return "err usage: hid usb|ble|auto|pair|unpair";
  if (cmd.kind == HID_CMD_QUERY) return String("ok ") + hid_pref_name(pref);
  if (cmd.kind == HID_CMD_SET) {
    return ble_hid_set_pref(cmd.pref) ? "ok" : "err hid release pending";
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
    return ble_hid_unpair() ? "ok" : "err hid unpair failed";
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
