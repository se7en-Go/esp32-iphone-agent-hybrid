// Transport choice, pace profile, status fields, and the keyboard usages BLE HID sends.
#include <stdio.h>
#include <string.h>
#include "../hid_bridge/src/hid/transport.h"
#include "../hid_bridge/src/hid/keys.h"

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

static hid_link_t link_of(int usb, int suspended, int conn, int bond) {
  hid_link_t link;
  memset(&link, 0, sizeof link);
  link.usb_mounted = usb;
  link.usb_suspended = suspended;
  link.ble_connected = conn;
  link.ble_bonded = bond;
  link.ble_advertising = 1;
  return link;
}

static void test_select(void) {
  hid_link_t usb = link_of(1, 0, 0, 0);
  hid_link_t asleep = link_of(1, 1, 0, 0);
  hid_link_t ble = link_of(0, 0, 1, 1);
  hid_link_t both = link_of(1, 0, 1, 1);
  hid_link_t none = link_of(0, 0, 0, 0);
  hid_link_t asleep_and_ble = link_of(1, 1, 1, 1);
  hid_link_t bond_only = link_of(0, 0, 0, 1);

  CHECK(hid_select(HID_PREF_AUTO, &usb) == HID_PATH_USB);
  CHECK(hid_select(HID_PREF_AUTO, &both) == HID_PATH_USB);          // cable wins
  CHECK(hid_select(HID_PREF_AUTO, &ble) == HID_PATH_BLE);
  CHECK(hid_select(HID_PREF_AUTO, &asleep_and_ble) == HID_PATH_BLE); // suspended cable is not USB
  CHECK(hid_select(HID_PREF_AUTO, &asleep) == HID_PATH_NONE);
  CHECK(hid_select(HID_PREF_AUTO, &none) == HID_PATH_NONE);
  CHECK(hid_select(HID_PREF_AUTO, &bond_only) == HID_PATH_NONE);    // bonded but not connected

  CHECK(hid_select(HID_PREF_USB, &both) == HID_PATH_USB);
  CHECK(hid_select(HID_PREF_USB, &ble) == HID_PATH_NONE);
  CHECK(hid_select(HID_PREF_BLE, &both) == HID_PATH_BLE);
  CHECK(hid_select(HID_PREF_BLE, &usb) == HID_PATH_NONE);
  CHECK(hid_select(HID_PREF_BLE, &bond_only) == HID_PATH_NONE);
}

static void test_errors(void) {
  hid_link_t usb = link_of(1, 0, 0, 0);
  hid_link_t asleep = link_of(1, 1, 0, 0);
  hid_link_t ble = link_of(0, 0, 1, 1);
  hid_link_t none = link_of(0, 0, 0, 0);
  CHECK(hid_ready_error(HID_PREF_AUTO, &usb) == NULL);
  CHECK(hid_ready_error(HID_PREF_AUTO, &ble) == NULL);
  CHECK(strcmp(hid_ready_error(HID_PREF_AUTO, &none), "err phone not connected over usb/ble") == 0);
  CHECK(strcmp(hid_ready_error(HID_PREF_AUTO, &asleep), "err phone usb asleep (unlock the phone)") == 0);
  CHECK(strcmp(hid_ready_error(HID_PREF_USB, &none), "err phone not connected over usb") == 0);
  CHECK(strcmp(hid_ready_error(HID_PREF_USB, &asleep), "err phone usb asleep (unlock the phone)") == 0);
  CHECK(strcmp(hid_ready_error(HID_PREF_USB, &ble), "err phone not connected over usb") == 0);
  CHECK(strcmp(hid_ready_error(HID_PREF_BLE, &usb), "err phone not connected over ble") == 0);
  CHECK(strcmp(hid_ready_error(HID_PREF_BLE, &none), "err phone not connected over ble") == 0);
  CHECK(hid_ready_error(HID_PREF_BLE, &ble) == NULL);
}

static void test_pace(void) {
  hid_pace_t usb, ble;
  hid_pace_profile(HID_PATH_USB, &usb);
  hid_pace_profile(HID_PATH_NONE, &ble); // "none" uses the USB numbers; nothing is being sent
  hid_pace_profile(HID_PATH_BLE, &ble);
  CHECK(usb.step == 10 && usb.gap_ms == 8 && usb.home_gap_ms == 2 && usb.version == HID_PACE_USB);
  CHECK(usb.conn_min == 0 && usb.conn_max == 0);
  CHECK(ble.step > usb.step && ble.gap_ms >= 15 && ble.home_gap_ms >= 15);
  CHECK(ble.version == HID_PACE_BLE);
  CHECK(ble.conn_min == 0 && ble.conn_max == 0); // stay on the 15 ms link; do not ask for 7.5 ms
  CHECK(ble.step <= 127);

  hid_link_t mounted = link_of(1, 0, 1, 1);
  hid_link_t only_ble = link_of(0, 0, 1, 1);
  hid_link_t none = link_of(0, 0, 0, 0);
  CHECK(hid_pace_version(HID_PREF_AUTO, hid_select(HID_PREF_AUTO, &mounted)) == HID_PACE_USB);
  CHECK(hid_pace_version(HID_PREF_AUTO, hid_select(HID_PREF_AUTO, &only_ble)) == HID_PACE_BLE);
  CHECK(hid_pace_version(HID_PREF_AUTO, hid_select(HID_PREF_AUTO, &none)) == HID_PACE_USB);
  CHECK(hid_pace_version(HID_PREF_BLE, HID_PATH_NONE) == HID_PACE_BLE);
  CHECK(hid_pace_version(HID_PREF_USB, HID_PATH_NONE) == HID_PACE_USB);
}

static void test_status_and_cmd(void) {
  hid_link_t link = link_of(0, 0, 1, 1);
  link.ble_advertising = 1;
  link.ble_pairing = 1;
  char buf[180];
  int n = hid_status_fragment(buf, sizeof buf, HID_PREF_AUTO, HID_PATH_BLE, &link);
  CHECK(n > 0 && (size_t)n < sizeof buf);
  CHECK(strstr(buf, "\"mode\":\"auto\"") && strstr(buf, "\"path\":\"ble\"") && strstr(buf, "\"pace\":4"));
  CHECK(strstr(buf, "\"adv\":1") && strstr(buf, "\"conn\":1") && strstr(buf, "\"bond\":1") && strstr(buf, "\"pair\":1"));
  CHECK(hid_status_compact(buf, sizeof buf, HID_PATH_NONE) > 0 && strcmp(buf, "\"hid\":\"none\"") == 0);
  CHECK(strcmp(hid_pref_name(hid_pref_parse("usb")), "usb") == 0);
  CHECK(hid_pref_parse("nope") < 0 && strcmp(hid_path_name(HID_PATH_USB), "usb") == 0);

  hid_cmd_t cmd;
  CHECK(hid_parse_cmd("", &cmd) == 0 && cmd.kind == HID_CMD_QUERY);
  CHECK(hid_parse_cmd("  ble", &cmd) == 0 && cmd.kind == HID_CMD_SET && cmd.pref == HID_PREF_BLE);
  CHECK(hid_parse_cmd("pair", &cmd) == 0 && cmd.kind == HID_CMD_PAIR);
  CHECK(hid_parse_cmd("pair off", &cmd) == 0 && cmd.kind == HID_CMD_PAIR_OFF);
  CHECK(hid_parse_cmd("unpair", &cmd) == 0 && cmd.kind == HID_CMD_UNPAIR);
  CHECK(hid_parse_cmd("wifi", &cmd) != 0 && cmd.kind == HID_CMD_BAD);
}

static void test_keys(void) {
  uint8_t usage = 0, mod = 0, keys[6];
  int shift = 0, n = 0;
  uint16_t cons = 0;
  CHECK(hid_ascii('a', &usage, &shift) && usage == 0x04 && !shift);
  CHECK(hid_ascii('A', &usage, &shift) && usage == 0x04 && shift);
  CHECK(hid_ascii('0', &usage, &shift) && usage == 0x27 && !shift);
  CHECK(hid_ascii('!', &usage, &shift) && usage == 0x1E && shift);
  CHECK(hid_ascii(' ', &usage, &shift) && usage == 0x2C && !shift);
  CHECK(hid_ascii('_', &usage, &shift) && usage == 0x2D && shift);
  CHECK(hid_ascii('\n', &usage, &shift) && usage == 0x28 && !shift);
  CHECK(hid_ascii('\r', &usage, &shift) && usage == 0x28 && !shift);
  CHECK(!hid_ascii('\0', &usage, &shift));
  CHECK(!hid_key_token("\0", 1, &mod, &usage, &cons));

  CHECK(hid_fill_combo("cmd+h", &mod, keys, &n, &cons) && mod == 0x08 && n == 1 && keys[0] == 0x0B && cons == 0);
  CHECK(hid_fill_combo("ctrl+space", &mod, keys, &n, &cons) && mod == 0x01 && n == 1 && keys[0] == 0x2C);
  CHECK(hid_fill_combo("enter", &mod, keys, &n, &cons) && n == 1 && keys[0] == 0x28 && mod == 0);
  CHECK(hid_fill_combo("backspace", &mod, keys, &n, &cons) && keys[0] == 0x2A);
  CHECK(hid_fill_combo("globe", &mod, keys, &n, &cons) && cons == 0x029D && n == 0);
  CHECK(hid_fill_combo("ime", &mod, keys, &n, &cons) && cons == 0x029D);
  CHECK(!hid_fill_combo("cmd+globe", &mod, keys, &n, &cons));
  CHECK(!hid_fill_combo("nope", &mod, keys, &n, &cons));
  CHECK(!hid_fill_combo("", &mod, keys, &n, &cons));
  CHECK(hid_fill_combo("A", &mod, keys, &n, &cons) && mod == 0x02 && keys[0] == 0x04);
}

int main(void) {
  test_select();
  test_errors();
  test_pace();
  test_status_and_cmd();
  test_keys();
  if (fails) printf("%d FAILED\n", fails);
  else printf("hid transport ok\n");
  return fails ? 1 : 0;
}
