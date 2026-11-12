#pragma once
// HID-over-GATT on the same NimBLE server as ble_link (one legacy advert, public identity).
// Not yet verified on a real iPhone. Bonds live in NVS namespace "hid".
#include <Arduino.h>
#include <stddef.h>
#include <stdint.h>

class NimBLEServer;

void ble_hid_begin(NimBLEServer *server);
String ble_hid_command(const char *arg); // "ok" / "ok <mode>" / "err ..."

bool ble_hid_connected(); // encrypted and subscribed to an input report
bool ble_hid_session_active(); // owning ACL still exists, even while reports are unavailable
bool ble_hid_using(uint16_t handle); // this connection is the one holding an input subscription
bool ble_hid_bonded();    // a HID bond is stored
bool ble_hid_pair_window_open();
void ble_hid_allow_pairing(uint32_t window_ms);
void ble_hid_deny_pairing();
void ble_hid_remember(uint8_t type, const uint8_t addr[6]);
void ble_hid_bonds_cleared(); // companion `unpair` already deleted the controller bonds

int ble_hid_pref();
bool ble_hid_set_pref(int pref);
bool hid_prepare_pref(int pref); // bridge releases held inputs before changing paths
bool ble_hid_unpair();

bool ble_hid_mouse(int8_t x, int8_t y, int8_t wheel, int8_t pan);
bool ble_hid_button(bool left_down);
void ble_hid_release_all();
bool ble_hid_keyboard(uint8_t modifier, const uint8_t *keys, int nkeys);
bool ble_hid_consumer(uint16_t usage); // 0 releases
void ble_hid_arm_interval(int tight);
void ble_hid_on_disconnect(uint16_t handle);
