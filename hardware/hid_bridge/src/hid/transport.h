#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Which pipe the board prefers. Stored in NVS. auto: USB when the phone has enumerated
// and is not suspended, otherwise a bonded HID central that is connected and subscribed.
enum { HID_PREF_USB = 0, HID_PREF_BLE = 1, HID_PREF_AUTO = 2 };
enum { HID_PATH_NONE = 0, HID_PATH_USB = 1, HID_PATH_BLE = 2 };

// Answered by "pace". 3 is the USB schedule this tree already calibrated against.
// 4 is the BLE schedule (fewer, larger steps, a longer gap). The host drops its
// saved span when this number changes.
enum { HID_PACE_USB = 3, HID_PACE_BLE = 4 };

typedef struct {
  int usb_mounted;     // phone enumerated the USB device
  int usb_suspended;   // enumerated, but the bus is suspended
  int ble_connected;   // encrypted (bonded) central subscribed to an input report
  int ble_bonded;      // a HID bond is stored, so the phone can reconnect after reboot
  int ble_advertising; // the public identity (the one carrying the HID UUID) is on air
  int ble_pairing;     // HID pairing window is open
  uint32_t ble_peer;   // opaque non-zero id of the connected bonded central, or 0
  uint32_t ble_pair_seq; // monotonic pairing/unpair generation persisted in NVS
} hid_link_t;

typedef struct {
  int step;       // pointer units per report when "move" omits a speed
  int gap_ms;     // pause after each pointer report
  int home_gap_ms;
  int version;    // HID_PACE_USB or HID_PACE_BLE
  int conn_min;   // 1.25 ms units. 0 means do not request a new interval (USB, and BLE stays at 15 ms)
  int conn_max;
} hid_pace_t;

enum {
  HID_CMD_QUERY = 0,
  HID_CMD_SET = 1,
  HID_CMD_PAIR = 2,
  HID_CMD_PAIR_OFF = 3,
  HID_CMD_UNPAIR = 4,
  HID_CMD_BAD = -1
};

typedef struct {
  int kind;
  int pref; // HID_CMD_SET
} hid_cmd_t;

int hid_pref_parse(const char *s); // -1 if it is not usb|ble|auto
const char *hid_pref_name(int pref);
const char *hid_path_name(int path);

int hid_select(int pref, const hid_link_t *link);
// Full reply, including "err ". NULL when a path is ready.
const char *hid_ready_error(int pref, const hid_link_t *link);

void hid_pace_profile(int path, hid_pace_t *out);
// Version the host should store. A chosen BLE path, or a BLE preference with
// nothing connected yet, is HID_PACE_BLE so a mode switch forces recalibration.
// auto/usb with nothing connected stays HID_PACE_USB so unplugging does not.
int hid_pace_version(int pref, int path);

// One JSON object member, no surrounding braces.
int hid_status_fragment(char *dst, size_t n, int pref, int path, const hid_link_t *link);
// Short form for a tiny MTU: "hid":"usb"|"ble"|"none"
int hid_status_compact(char *dst, size_t n, int path);

// "usb"|"ble"|"auto"|"pair"|"pair off"|"unpair"|"" (query). 0 on success.
int hid_parse_cmd(const char *arg, hid_cmd_t *out);

#ifdef __cplusplus
}
#endif
