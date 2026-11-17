#include "transport.h"

#include <stdio.h>
#include <string.h>

int hid_pref_parse(const char *s) {
  if (!s) return -1;
  if (strcmp(s, "usb") == 0) return HID_PREF_USB;
  if (strcmp(s, "ble") == 0) return HID_PREF_BLE;
  if (strcmp(s, "auto") == 0) return HID_PREF_AUTO;
  return -1;
}

const char *hid_pref_name(int pref) {
  if (pref == HID_PREF_USB) return "usb";
  if (pref == HID_PREF_BLE) return "ble";
  return "auto";
}

const char *hid_path_name(int path) {
  if (path == HID_PATH_USB) return "usb";
  if (path == HID_PATH_BLE) return "ble";
  return "none";
}

static int usb_live(const hid_link_t *link) { return link->usb_mounted && !link->usb_suspended; }

int hid_select(int pref, const hid_link_t *link) {
  int usb = usb_live(link);
  int ble = link->ble_connected;
  if (pref == HID_PREF_USB) return usb ? HID_PATH_USB : HID_PATH_NONE;
  if (pref == HID_PREF_BLE) return ble ? HID_PATH_BLE : HID_PATH_NONE;
  if (usb) return HID_PATH_USB;
  if (ble) return HID_PATH_BLE;
  return HID_PATH_NONE;
}

const char *hid_ready_error(int pref, const hid_link_t *link) {
  if (hid_select(pref, link) != HID_PATH_NONE) return NULL;
  if (pref == HID_PREF_BLE) return "err phone not connected over ble";
  if (pref == HID_PREF_USB) {
    if (link->usb_mounted && link->usb_suspended) return "err phone usb asleep (unlock the phone)";
    return "err phone not connected over usb";
  }
  // auto, and neither pipe is usable. A suspended cable is the old asleep case
  // when Bluetooth is not up either: unlocking is the next step.
  if (link->usb_mounted && link->usb_suspended) return "err phone usb asleep (unlock the phone)";
  return "err phone not connected over usb/ble";
}

void hid_pace_profile(int path, hid_pace_t *out) {
  if (path == HID_PATH_BLE) {
    // Fewer, larger steps than USB. The link stays at the 15 ms interval
    // tuneLink already asked for: Apple rejects anything under 11.25 ms, and
    // widening it after a gesture would slow the GATT video on the same ACL.
    out->step = 30;
    out->gap_ms = 16;
    out->home_gap_ms = 16;
    out->version = HID_PACE_BLE;
    out->conn_min = 0;
    out->conn_max = 0;
    return;
  }
  out->step = 10;
  out->gap_ms = 8;
  out->home_gap_ms = 2;
  out->version = HID_PACE_USB;
  out->conn_min = 0;
  out->conn_max = 0;
}

int hid_pace_version(int pref, int path) {
  if (path == HID_PATH_BLE || (path == HID_PATH_NONE && pref == HID_PREF_BLE)) return HID_PACE_BLE;
  return HID_PACE_USB;
}

int hid_status_fragment(char *dst, size_t n, int pref, int path, const hid_link_t *link) {
  return snprintf(dst, n,
                  "\"hid\":{\"mode\":\"%s\",\"path\":\"%s\",\"pace\":%d,\"middle_click\":1,"
                  "\"ble\":{\"adv\":%d,\"conn\":%d,\"bond\":%d,\"pair\":%d}}",
                  hid_pref_name(pref), hid_path_name(path), hid_pace_version(pref, path),
                  link->ble_advertising ? 1 : 0, link->ble_connected ? 1 : 0, link->ble_bonded ? 1 : 0,
                  link->ble_pairing ? 1 : 0);
}

int hid_status_compact(char *dst, size_t n, int path) {
  return snprintf(dst, n, "\"hid\":\"%s\"", hid_path_name(path));
}

static const char *trim(const char *s) {
  while (*s == ' ' || *s == '\t') s++;
  return s;
}

int hid_parse_cmd(const char *arg, hid_cmd_t *out) {
  const char *s = trim(arg ? arg : "");
  out->kind = HID_CMD_BAD;
  out->pref = HID_PREF_AUTO;
  if (*s == 0) {
    out->kind = HID_CMD_QUERY;
    return 0;
  }
  if (strcmp(s, "pair") == 0) {
    out->kind = HID_CMD_PAIR;
    return 0;
  }
  if (strcmp(s, "pair off") == 0) {
    out->kind = HID_CMD_PAIR_OFF;
    return 0;
  }
  if (strcmp(s, "unpair") == 0) {
    out->kind = HID_CMD_UNPAIR;
    return 0;
  }
  int pref = hid_pref_parse(s);
  if (pref < 0) return -1;
  out->kind = HID_CMD_SET;
  out->pref = pref;
  return 0;
}
