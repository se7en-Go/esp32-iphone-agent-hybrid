// Run the production HID implementation; fake only NVS, time and NimBLE boundaries.
#include <cassert>
#include <cstdio>
#include "../hid_bridge/ble_hid.cpp"
#include "../hid_bridge/src/ble/advertising_gate.h"

static BleAdvertisingGate gate;
static bool advertising = true, paused, stop_ok = true;
static bool actual_bonds[8];
static bool our_bonds[8];
static bool store_read_error;
static bool prepare_pref_ok = true;
static int fail_bond = -1, retained_bond = -1, forgotten, deletes, busy_deletes, restarts;
void board_log(const char *, ...) {}
void ble_forget_addr(uint8_t, const uint8_t[6]) { forgotten++; }
bool hid_prepare_pref(int) { return prepare_pref_ok; }
static void background_advertise() { gate.run([] { advertising = true; restarts++; }); }
bool ble_pause_advertising() {
  return gate.pause([] {
    if (!stop_ok) return false;
    paused = true;
    advertising = false;
    return true;
  });
}
void ble_resume_advertising() { gate.resume(); paused = false; background_advertise(); }
bool test_delete_bond(const NimBLEAddress &addr) {
  deletes++;
  background_advertise();  // TX task / disconnect callback attempts to restart during unpair.
  if (advertising) { busy_deletes++; return false; } // IRK + advertising: BLE_HS_EBUSY.
  int id = addr.getVal()[5];
  if (id == fail_bond) return false;
  our_bonds[id] = false; // delete_peer removes OUR_SEC before PEER_SEC.
  if (id != retained_bond) actual_bonds[id] = false;
  return true;
}
int ble_store_read_our_sec(const ble_store_key_sec *key, ble_store_value_sec *) {
  if (store_read_error) return 8;
  return our_bonds[key->peer_addr.val[5]] ? 0 : BLE_HS_ENOENT;
}
int ble_store_read_peer_sec(const ble_store_key_sec *key, ble_store_value_sec *) {
  if (store_read_error) return 8;
  return actual_bonds[key->peer_addr.val[5]] ? 0 : BLE_HS_ENOENT;
}

static void reset() {
  gate.resume();
  ble_hid_bonds_cleared();
  clock_ms = 0;
  delay_hook = nullptr;
  connected = true;
  free_blocks = 64;
  allocation_failures = notify_failures = notify_rc = allocations = 0;
  sent.clear();
  advertising = true;
  paused = false;
  stop_ok = true;
  store_read_error = false;
  prepare_pref_ok = true;
  memset(actual_bonds, 0, sizeof actual_bonds);
  memset(our_bonds, 0, sizeof our_bonds);
  fail_bond = retained_bond = -1;
  forgotten = deletes = busy_deletes = restarts = 0;
  NimBLEConnInfo ci(1);
  mouseIn->cb->onSubscribe(mouseIn, ci, 1);
  keyIn->cb->onSubscribe(keyIn, ci, 1);
  consIn->cb->onSubscribe(consIn, ci, 1);
  actual_bonds[1] = our_bonds[1] = true;
}

static void test_synchronous_mbuf_release() {
  reset();
  uint8_t key = 4;
  assert(ble_hid_keyboard(0, &key, 1));
  assert(ble_hid_keyboard(0, nullptr, 0) && "key-up must follow synchronously freed key-down");
  for (int i = 0; i < 100; i++) assert(ble_hid_mouse(1, 0, 0, 0));
  assert(ble_hid_button(true));
  assert(ble_hid_button(false));
  assert(ble_hid_consumer(0x029D));
  assert(ble_hid_consumer(0));
  assert(sent.size() == 106 && free_blocks == 64);
  assert(sent[0].data[2] == key && sent[1].data[2] == 0);
  puts("ok BLE synchronous mbuf release: key-down/up and continuous reports");
}

static void test_buffer_recovery() {
  reset();
  // HID reports require two blocks plus the accepted sixteen-block reserve.
  free_blocks = 17;
  assert(!ble_hid_button(true));
  assert(allocations == 0 && sent.empty() && clock_ms == 40);
  free_blocks = 18;
  assert(ble_hid_button(true));
  free_blocks = 0;
  assert(!ble_hid_button(false));
  assert(buttons == 1 && "failed release must remain pending");
  uint32_t recover_at = clock_ms + 6;
  static uint32_t recovery;
  recovery = recover_at;
  delay_hook = [] { if (clock_ms >= recovery) free_blocks = 18; };
  assert(ble_hid_button(false));
  delay_hook = nullptr;
  allocation_failures = 2;
  notify_failures = 2;
  assert(ble_hid_mouse(1, 0, 0, 0));
  notify_rc = 7;
  assert(!ble_hid_mouse(1, 0, 0, 0));
  notify_rc = 0;
  assert(ble_hid_mouse(1, 0, 0, 0));
  assert(free_blocks == 18);
  puts("ok BLE reserve, allocation/notify failures and recovery");
}

static void test_unpair_with_advertising() {
  reset();
  assert(ble_hid_command("unpair") == "ok");
  assert(!actual_bonds[1] && !ble_hid_bonded() && store.saved_count == 0);
  assert(deletes == 1 && busy_deletes == 0 && forgotten == 1);
  assert(advertising && !paused && restarts == 1);
  puts("ok unpair pauses background advertising and restores it");
}

static void test_unpair_failures() {
  reset();
  stop_ok = false;
  assert(ble_hid_command("unpair").find("err ") == 0);
  assert(actual_bonds[1] && ble_hid_bonded() && store.saved_count == 1 && deletes == 0);
  assert(advertising && !paused);
  stop_ok = true;
  store_read_error = true;
  assert(ble_hid_command("unpair").find("err ") == 0);
  assert(actual_bonds[1] && ble_hid_bonded() && store.saved_count == 1 && deletes == 0);
  assert(advertising && !paused);
  store_read_error = false;
  fail_bond = 1;
  assert(ble_hid_command("unpair").find("err ") == 0);
  assert(actual_bonds[1] && ble_hid_bonded() && store.saved_count == 1 && forgotten == 0);
  assert(advertising && !paused);
  fail_bond = -1;
  retained_bond = 1; // Some NimBLE storage errors can be logged without reaching deleteBond's return.
  assert(ble_hid_command("unpair").find("err ") == 0);
  assert(actual_bonds[1] && ble_hid_bonded() && store.saved_count == 1 && forgotten == 0);
  assert(!our_bonds[1] && "PEER_SEC left by a partial storage delete must retain the mirror");
  retained_bond = -1;
  assert(ble_hid_command("unpair") == "ok");
  assert(!actual_bonds[1] && !ble_hid_bonded() && forgotten == 1);
  puts("ok unpair failures retain records and allow retry");
}

static void test_partial_unpair() {
  reset();
  uint8_t addr[6] = {0, 0, 0, 0, 0, 2};
  ble_hid_remember(0, addr);
  actual_bonds[2] = our_bonds[2] = true;
  fail_bond = 2;
  assert(ble_hid_command("unpair").find("err ") == 0);
  assert(!actual_bonds[1] && actual_bonds[2] && bondCount == 1 && store.saved_count == 1);
  assert(bonds[0].val[5] == 2 && forgotten == 1 && advertising && !paused);
  fail_bond = -1;
  assert(ble_hid_command("unpair") == "ok");
  assert(!actual_bonds[2] && !ble_hid_bonded() && forgotten == 2);
  puts("ok partial unpair removes only successful records");
}

static void test_mode_change_release_failure() {
  reset();
  assert(ble_hid_command("ble") == "ok");
  assert(pref == HID_PREF_BLE && store.saved_mode == HID_PREF_BLE);
  prepare_pref_ok = false;
  assert(ble_hid_command("usb") == "err hid release pending");
  assert(pref == HID_PREF_BLE && store.saved_mode == HID_PREF_BLE);
  prepare_pref_ok = true;
  assert(ble_hid_command("usb") == "ok");
  assert(pref == HID_PREF_USB && store.saved_mode == HID_PREF_USB);
  puts("ok failed path release preserves preferred and persisted mode");
}

int main(int argc, char **argv) {
  gate.begin();
  ble_hid_begin(NimBLEDevice::getServer());
  assert(!ble_hid_pair_window_open());
  if (argc == 1 || strcmp(argv[1], "send") == 0) {
    test_synchronous_mbuf_release();
    test_buffer_recovery();
  }
  if (argc == 1 || strcmp(argv[1], "unpair") == 0) {
    test_unpair_with_advertising();
    test_unpair_failures();
    test_partial_unpair();
  }
  if (argc == 1) test_mode_change_release_failure();
}
