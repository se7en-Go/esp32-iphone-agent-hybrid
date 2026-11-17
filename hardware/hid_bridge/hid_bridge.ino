// 混合项目（协议未改）：本目录来自 ESP32 iPhone Agent by Henry Yuan
// https://github.com/onehumanbeing/esp32-iphone-agent  commit 5a4fddf
// N16R8 的编译选项在 hardware/build_firmware.sh：CDCOnBoot=default，串口留在 COM 口。
// 出厂 NVS 仍默认 AP；电脑经串口写入 STA 后，才是给使用者的默认网络。见仓库 ARCHITECTURE.md。

// HID bridge: an ESP32-S3 (XIAO, 8 MB PSRAM) plugged into the iPhone. It is the phone's
// USB mouse + keyboard, an AirPlay screen-mirroring receiver, and a BLE peripheral for the
// iOS app on the same phone. It holds no logic; the app decides. Protocol: docs/ble-protocol.md.
//
// HID commands (unchanged from the old TCP protocol, the board answers "ok" or "err <why>"):
//   ping | pace | home | move <dx> <dy> [speed] | press [left|middle] | release | click [left|middle]
//   scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter>
//   homescreen (the consumer-control Home key, "AC Home": iOS goes to the Home Screen, like a keyboard's home key)

#include <esp_task_wdt.h>
#include <esp_mac.h>
#include <esp_wifi.h>
#include <esp_core_dump.h>
#include <atomic>
#include "USB.h"
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include "tusb.h"
#include "ble_link.h"
#include "netcfg.h"
#include "video_link.h"
#include "cli_link.h"
#include "cli_tcp.h"
#include "wifi_video.h"
#include "ota.h"
#include "version.h"
#include "src/airplay/airplay.h"
#include "src/hid/arbiter.h"
#include "src/hid/pace.h"
#include "src/hid/transport.h"
#include "src/hid/keys.h"
#include "src/hid/command_line.h"
#include "ble_hid.h"
#include "src/heard/heard.h"
#include "src/cli/cli_proto.h"
#include "logbuf.h"

const int HOME_REPORTS = 25;  // per axis. The gap and the default step come from hid_pace_profile.
#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0  // 1: echo commands and the AirPlay handshake over USB serial
#endif

const int WATCHDOG_MS = 8000;  // a hung loop (e.g. blocked USB) reboots the board instead of staying offline

USBHIDMouse mouse;
USBHIDKeyboard keyboard;
USBHIDConsumerControl consumer;
const uint16_t AC_HOME = 0x0223;  // HID consumer page: AC Home
static String receiverName = "ESP32 Agent";

static int8_t clamp8(long v) {
  if (v > 127) return 127;
  if (v < -127) return -127;
  return (int8_t)v;
}

static hid_link_t hidFacts() {
  hid_link_t link = {};
  link.usb_mounted = tud_mounted() ? 1 : 0;
  link.usb_suspended = tud_suspended() ? 1 : 0;
  link.ble_connected = ble_hid_connected() ? 1 : 0;
  link.ble_bonded = ble_hid_bonded() ? 1 : 0;
  link.ble_advertising = ble_app_advertising() ? 1 : 0;
  link.ble_pairing = ble_hid_pair_window_open() ? 1 : 0;
  return link;
}

// Only the command/loop task touches this state. Keep the report's originating
// path until its release is accepted or that connection ends. A failed release
// quarantines only its own route after a short grace period, never the other host.
enum { HELD_MOUSE = 1, HELD_KEYS = 2, HELD_CONSUMER = 4, HELD_ALL = 7 };
static const uint8_t HID_MOUSE_MIDDLE_BTN = 0x04;
static uint8_t heldReports[3];
static uint8_t usbMouseButtons = 0;
static bool releasePending[3];
static uint32_t releaseStarted[3], releaseLastTry[3];
// statusTask only reads the quarantine flags; release bookkeeping stays on loop().
static std::atomic<bool> releaseDeferred[3];
static std::atomic<bool> usbSessionEnded;
static const uint32_t HID_RELEASE_WAIT_MS = 250, HID_RELEASE_RETRY_MS = 50;

static void usbHidStopped(void *, esp_event_base_t, int32_t, void *) {
  // Do not touch report state from the USB event task. Remember even a disconnect
  // followed by a reconnect before the next loop observes tud_mounted().
  usbSessionEnded = true;
}

static void clearReleaseState(int path) {
  heldReports[path] = 0;
  if (path == HID_PATH_USB) usbMouseButtons = 0;
  releasePending[path] = releaseDeferred[path] = false;
}

static void markReleasePending(int path) {
  if (!releasePending[path]) releaseStarted[path] = millis();
  releasePending[path] = true;
}

static bool releaseIsDeferred(int path) {
  if (releasePending[path] && (uint32_t)(millis() - releaseStarted[path]) >= HID_RELEASE_WAIT_MS)
    releaseDeferred[path] = true;
  return releasePending[path] && releaseDeferred[path];
}

static bool sendUsbMouseReport(uint8_t buttons, int8_t x = 0, int8_t y = 0,
                               int8_t wheel = 0, int8_t pan = 0) {
  // Keep button state in the route bookkeeping, never Arduino's mouse cache.
  // A long move may outlive its USB session before loop() handles the stop event.
  hid_mouse_report_t report = {};
  report.buttons = usbSessionEnded ? 0 : buttons;
  report.x = x;
  report.y = y;
  report.wheel = wheel;
  report.pan = pan;
  return mouse.sendReport(report);
}

static void moveUsbMouse(int8_t x, int8_t y, int8_t wheel = 0, int8_t pan = 0) {
  sendUsbMouseReport(heldReports[HID_PATH_USB] & HELD_MOUSE ? usbMouseButtons : 0,
                     x, y, wheel, pan);
}

static bool releaseReports(int path, uint8_t reports) {
  // A detached USB device / ended BLE ACL no longer owns host key state.
  // Suspend, lost encryption and lost subscriptions do not end that session.
  bool ended = path == HID_PATH_USB && usbSessionEnded.exchange(false);
  bool alive = path == HID_PATH_USB ? tud_mounted() : ble_hid_session_active();
  if (!alive) {
    if (path == HID_PATH_USB && heldReports[path]) {
      // Mouse reports have no Arduino cache; discard the old keyboard cache too.
      keyboard.releaseAll();
    }
    clearReleaseState(path);
    return true;
  }
  // Reattachment still needs a checked zero report. Keep failed cleanup pending
  // just like an ordinary release, even if the caller supplied no report mask.
  if (ended) reports |= heldReports[path];
  if (!reports) {
    if (!heldReports[path]) clearReleaseState(path);
    return true;
  }
  releaseLastTry[path] = millis();
  bool ready = path == HID_PATH_USB ? tud_mounted() && !tud_suspended() : ble_hid_connected();
  if (!ready) {
    heldReports[path] |= reports;
    markReleasePending(path);
    return false;
  }
  bool ok = true;
  for (uint8_t bit = HELD_MOUSE; bit <= HELD_CONSUMER; bit <<= 1) {
    if (!(reports & bit)) continue;
    bool sent = true;
    if (path == HID_PATH_BLE) {
      if (bit == HELD_MOUSE) sent = ble_hid_buttons(0);
      else if (bit == HELD_KEYS) sent = ble_hid_keyboard(0, NULL, 0);
      else sent = ble_hid_consumer(0);
    } else {
      if (bit == HELD_MOUSE) {
        sent = sendUsbMouseReport(0);
        if (sent) usbMouseButtons = 0;
      }
      else if (bit == HELD_KEYS) keyboard.releaseAll();
      else sent = consumer.release() != 0;
      sent = sent && tud_mounted() && !tud_suspended();
    }
    if (sent) heldReports[path] &= ~bit;
    else { heldReports[path] |= bit; ok = false; }
  }
  if (!ok) markReleasePending(path);
  else if (!heldReports[path]) clearReleaseState(path);
  return ok;
}

static bool releasePath(int path, bool force, uint8_t mask) {
  uint8_t reports = heldReports[path] & mask;
  bool ready = path == HID_PATH_USB ? tud_mounted() && !tud_suspended() : ble_hid_connected();
  if (force && ready) reports = mask;
  return releaseReports(path, reports);
}

// Emergency stop bypasses selection and phoneReady: a live alternate route must
// still be released when the preferred route is absent. Try both even on failure.
static bool releaseHidReports(uint8_t reports) {
  bool usb = releasePath(HID_PATH_USB, true, reports);
  bool ble = releasePath(HID_PATH_BLE, true, reports);
  return usb && ble;
}

static bool releaseHid() { return releaseHidReports(HELD_ALL); }

static bool syncHidPath(int next) {
  bool ok = true;
  for (int path = HID_PATH_USB; path <= HID_PATH_BLE; path++) {
    if (path == next && !releasePending[path]) continue;
    // Only background retries are throttled; explicit release always attempts
    // both routes. Keep the pending bits even after allowing an alternate route.
    bool deferred = releaseIsDeferred(path);
    bool alive = path == HID_PATH_USB ? tud_mounted() : ble_hid_session_active();
    if (!deferred || !alive || (uint32_t)(millis() - releaseLastTry[path]) >= HID_RELEASE_RETRY_MS)
      releasePath(path, false, HELD_ALL);
    if (releasePending[path] && (path == next || !releaseIsDeferred(path))) ok = false;
  }
  return ok;
}

static int selectHidPath(int pref, hid_link_t link) {
  // AUTO can continue on the healthy route while the preferred one is waiting
  // for a release. Never send new input on the route whose zero report failed.
  if (pref == HID_PREF_AUTO) {
    if (releaseDeferred[HID_PATH_USB]) link.usb_mounted = 0;
    if (releaseDeferred[HID_PATH_BLE]) link.ble_connected = 0;
  }
  return hid_select(pref, &link);
}

static bool prepareHidPath(int pref, const hid_link_t &link, int &next) {
  if (usbSessionEnded) releaseReports(HID_PATH_USB, 0);
  for (int path = HID_PATH_USB; path <= HID_PATH_BLE; path++) releaseIsDeferred(path);
  next = selectHidPath(pref, link);
  bool ok = syncHidPath(next);
  // A send attempt may have exhausted the grace period. Reconsider AUTO once.
  int after = selectHidPath(pref, link);
  if (after != next) {
    next = after;
    ok = syncHidPath(next);
  }
  return ok && !(next == HID_PATH_NONE && hid_select(pref, &link) != HID_PATH_NONE);
}

// Called by ble_hid_set_pref before saving or acknowledging an explicit switch.
bool hid_prepare_pref(int next) {
  hid_link_t link = hidFacts();
  int path;
  return prepareHidPath(next, link, path);
}

static int activePath() {
  hid_link_t link = hidFacts();
  int next;
  return prepareHidPath(ble_hid_pref(), link, next) ? next : HID_PATH_NONE;
}

static uint8_t parseMouseButton(const String &raw) {
  const char *s = raw.c_str();
  while (*s == ' ' || *s == '\t' || *s == '\r' || *s == '\n') s++;
  size_t n = strlen(s);
  while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\r' || s[n - 1] == '\n')) n--;
  if (n == 0 || (n == 4 && memcmp(s, "left", 4) == 0)) return MOUSE_LEFT;
  if (n == 6 && memcmp(s, "middle", 6) == 0) return HID_MOUSE_MIDDLE_BTN;
  return 0;
}

static bool buttonMask(int path, uint8_t mask) {
  if (path == HID_PATH_NONE) return false;
  if (!mask) return releaseReports(path, HELD_MOUSE);
  if (path == HID_PATH_BLE) {
    if (!ble_hid_buttons(mask)) return false;
  } else {
    usbMouseButtons = mask;
    sendUsbMouseReport(mask);
  }
  heldReports[path] |= HELD_MOUSE;
  return true;
}

static bool buttonOn(int path, bool down) {
  return buttonMask(path, down ? MOUSE_LEFT : 0);
}

static bool bleKeyboard(uint8_t mod, const uint8_t *keys, int n) {
  if (!mod && !n) return releaseReports(HID_PATH_BLE, HELD_KEYS);
  if (!ble_hid_keyboard(mod, keys, n)) return false;
  heldReports[HID_PATH_BLE] |= HELD_KEYS;
  return true;
}

static bool bleConsumer(uint16_t usage) {
  if (!usage) return releaseReports(HID_PATH_BLE, HELD_CONSUMER);
  if (!ble_hid_consumer(usage)) return false;
  heldReports[HID_PATH_BLE] |= HELD_CONSUMER;
  return true;
}

// Wake a suspended USB phone only when USB is the path we would use.
// Waking it while BLE is selected makes the next select() flip to USB,
// which changes the pace and drops the BLE calibration.
// The error string is the whole reply ("err ...").
static bool phoneReady(const char **why) {
  int pref = ble_hid_pref();
  hid_link_t link = hidFacts();
  if (pref != HID_PREF_BLE && hid_select(pref, &link) != HID_PATH_BLE && tud_suspended()) {
    tud_remote_wakeup();
    for (int i = 0; i < 50 && tud_suspended(); i++) delay(10);
    link = hidFacts();
  }
  int next;
  if (!prepareHidPath(pref, link, next)) {
    if (why) *why = "err hid release pending";
    return false;
  }
  const char *err = hid_ready_error(pref, &link);
  if (err) {
    if (why) *why = err;
    return false;
  }
  return true;
}

static bool movePaced(long dx, long dy, int step) {
  int path = activePath();
  hid_pace_t prof;
  hid_pace_profile(path, &prof);
  if (path == HID_PATH_BLE) ble_hid_arm_interval(1);
  pace_t p;
  pace_start(&p, dx, dy, step);
  int sx, sy;
  bool ok = true;
  while (ok && pace_next(&p, &sx, &sy)) {
    if (path == HID_PATH_BLE) ok = ble_hid_mouse(clamp8(sx), clamp8(sy), 0, 0);
    else if (path == HID_PATH_USB) moveUsbMouse(sx, sy);
    else ok = false;
    delay(prof.gap_ms);
  }
  if (path == HID_PATH_BLE) ble_hid_arm_interval(0);
  return ok;
}

static bool slamHome() {
  int path = activePath();
  hid_pace_t prof;
  hid_pace_profile(path, &prof);
  if (path == HID_PATH_BLE) ble_hid_arm_interval(1);
  bool ok = true;
  for (int axis = 0; axis < 2 && ok; axis++) {
    for (int i = 0; i < HOME_REPORTS && ok; i++) {
      int8_t x = axis == 0 ? -127 : 0;
      int8_t y = axis == 0 ? 0 : -127;
      if (path == HID_PATH_BLE) ok = ble_hid_mouse(x, y, 0, 0);
      else if (path == HID_PATH_USB) moveUsbMouse(x, y);
      else ok = false;
      delay(prof.home_gap_ms);
    }
  }
  if (path == HID_PATH_BLE) ble_hid_arm_interval(0);
  return ok;
}

uint8_t keyCode(const String &k) {
  if (k == "enter") return KEY_RETURN;
  if (k == "esc") return KEY_ESC;
  if (k == "tab") return KEY_TAB;
  if (k == "backspace") return KEY_BACKSPACE;
  if (k == "space") return ' ';
  if (k == "up") return KEY_UP_ARROW;
  if (k == "down") return KEY_DOWN_ARROW;
  if (k == "left") return KEY_LEFT_ARROW;
  if (k == "right") return KEY_RIGHT_ARROW;
  if (k == "cmd") return KEY_LEFT_GUI;
  if (k == "ctrl") return KEY_LEFT_CTRL;
  if (k == "alt") return KEY_LEFT_ALT;
  if (k == "shift") return KEY_LEFT_SHIFT;
  return k.length() == 1 ? k[0] : 0;
}

// 1 ok, 0 unknown key, -1 the BLE notify was not accepted.
static int pressCombo(String combo) {
  uint8_t mod = 0, keys[6] = {};
  int n = 0;
  uint16_t cons = 0;
  if (!hid_fill_combo(combo.c_str(), &mod, keys, &n, &cons)) {
    releaseReports(HID_PATH_USB, heldReports[HID_PATH_USB] & HELD_KEYS);
    return 0;
  }
  int path = activePath();
  if (path == HID_PATH_BLE) {
    ble_hid_arm_interval(1);
    bool down = cons ? bleConsumer(cons) : bleKeyboard(mod, keys, n);
    delay(30);
    bool up = cons ? bleConsumer(0) : bleKeyboard(0, NULL, 0);
    ble_hid_arm_interval(0);
    return down && up ? 1 : -1;
  }
  if (path != HID_PATH_USB) return -1;
  if (cons) {
    heldReports[HID_PATH_USB] |= HELD_CONSUMER;
    consumer.press(cons);
    delay(30);
    return releaseReports(HID_PATH_USB, HELD_CONSUMER) ? 1 : -1;
  }
  // USB keeps the Arduino keymap. The tokens hid_fill_combo accepted are the same names keyCode maps.
  combo += '+';
  for (int i; (i = combo.indexOf('+')) >= 0; combo = combo.substring(i + 1)) {
    uint8_t c = keyCode(combo.substring(0, i));
    if (!c) { releaseReports(HID_PATH_USB, HELD_KEYS); return 0; }
    heldReports[HID_PATH_USB] |= HELD_KEYS;
    keyboard.press(c);
  }
  delay(30);
  return releaseReports(HID_PATH_USB, HELD_KEYS) ? 1 : -1;
}

// 1 ok, 0 unknown before anything was sent, -1 the link dropped before the first
// character, -2 some characters were sent (or a key-up failed) so a retry would repeat them.
static int bleType(const String &text) {
  ble_hid_arm_interval(1);
  int sent = 0;
  for (unsigned i = 0; i < text.length(); i++) {
    uint8_t usage = 0;
    int shift = 0;
    if (!hid_ascii(text[i], &usage, &shift)) {
      bleKeyboard(0, NULL, 0);
      ble_hid_arm_interval(0);
      return sent ? -2 : 0;
    }
    uint8_t one[6] = {usage};
    if (!bleKeyboard(shift ? 0x02 : 0, one, 1)) {
      bleKeyboard(0, NULL, 0);
      ble_hid_arm_interval(0);
      return sent ? -2 : -1;
    }
    sent++;
    delay(12);
    if (!bleKeyboard(0, NULL, 0)) {
      ble_hid_arm_interval(0);
      return -2;
    }
    delay(8);
  }
  ble_hid_arm_interval(0);
  return 1;
}

// A locked or sleeping phone suspends USB; sending HID reports then can stall. Ask it to wake up instead,
// and report "asleep" right away if it does not, so the board never hangs.
String run(const String &line) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0, c = 0;
  int got = sscanf(arg.c_str(), "%ld %ld %ld", &a, &b, &c);

  if (cmd == "ping") return "ok";
  if (cmd == "pace") {  // per transport, so the host recalibrates when the pipe changes
    hid_link_t link = hidFacts();
    int pref = ble_hid_pref();
    return String("ok ") + hid_pace_version(pref, selectHidPath(pref, link));
  }
  if (cmd == "release") return releaseHid() ? "ok" : "err hid release pending";
  const char *why = NULL;
  if (!phoneReady(&why)) return why ? why : "err phone not connected over usb/ble";
  int path = activePath();
  const char *down = path == HID_PATH_BLE ? "err phone not connected over ble" : "err phone not connected over usb";
  if (cmd == "home") {  // left first, then up the left edge: riding the top edge opened Control Center (2026-11-06)
    return slamHome() ? "ok" : down;
  }
  if (cmd == "move") {
    hid_pace_t prof;
    hid_pace_profile(path, &prof);
    int step = got >= 3 ? (int)c : prof.step;
    if (step < 1) step = 1;
    if (step > 40) step = 40;
    return movePaced(a, b, step) ? "ok" : down;
  }
  if (cmd == "press") {
    uint8_t mask = parseMouseButton(arg);
    if (!mask) return "err unknown button";
    return buttonMask(path, mask) ? "ok" : down;
  }
  if (cmd == "click") {
    uint8_t mask = parseMouseButton(arg);
    if (!mask) return "err unknown button";
    if (!buttonMask(path, mask)) return down;
    delay(60);
    return buttonMask(path, 0) ? "ok" : down;
  }
  if (cmd == "scroll") {
    if (path == HID_PATH_BLE) return ble_hid_mouse(0, 0, clamp8(a), clamp8(b)) ? "ok" : down;
    if (path != HID_PATH_USB) return down;
    moveUsbMouse(0, 0, a, b);
    return "ok";
  }
  if (cmd == "type") {
    if (path == HID_PATH_USB) {
      heldReports[HID_PATH_USB] |= HELD_KEYS;
      keyboard.print(arg);
      return releaseReports(HID_PATH_USB, HELD_KEYS) ? "ok" : "err partial type";
    }
    if (path != HID_PATH_BLE) return down;
    int typed = bleType(arg);
    if (typed == -2) return "err partial type";
    if (typed < 0) return down;
    return typed ? "ok" : "err unknown key";
  }
  if (cmd == "key") {
    int pressed = pressCombo(arg);
    if (pressed < 0) return down;
    return pressed ? "ok" : "err unknown key";
  }
  if (cmd == "homescreen") {
    if (path == HID_PATH_BLE) {
      if (!bleConsumer(AC_HOME)) return down;
      delay(40);
      if (!bleConsumer(0)) return down;
      return "ok";
    }
    if (path != HID_PATH_USB) return down;
    heldReports[HID_PATH_USB] |= HELD_CONSUMER;
    consumer.press(AC_HOME);
    delay(40);
    return releaseReports(HID_PATH_USB, HELD_CONSUMER) ? "ok" : down;
  }
  return "err unknown command";
}

static arb_state arb;  // the app or the CLI drives the phone, never both: src/hid/arbiter.h
const char *const APP_BUSY = "err busy: the app is using the board";

// An HID command from who; the side that leaves lets go of the button it held (loop()).
String drive(const String &line, char who) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String reply = run(line);
  arb_after(&arb, who == FROM_CLI ? ARB_CLI : ARB_APP, cmd.c_str(), reply == "ok");
  return reply;
}

const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_TASK_WDT: case ESP_RST_INT_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_SW: return "software";
    case ESP_RST_USB: return "usb";
    default: return "other";
  }
}

const char *usbState() {
  if (!tud_mounted()) return "off";
  return tud_suspended() ? "asleep" : "ok";
}

// The last crash saved in the core dump partition (kept until the next one): task, PC, backtrace. Read at boot,
// so a crash on a board plugged into the phone can be looked up over BLE (`crash`) without a serial cable.
static String lastCrash = "none";

void readCrash() {
  auto *c = (esp_core_dump_summary_t *)malloc(sizeof(esp_core_dump_summary_t));
  if (c && esp_core_dump_image_check() == ESP_OK && esp_core_dump_get_summary(c) == ESP_OK) {
    char b[200];
    int n = snprintf(b, sizeof b, "%.16s pc %08lx bt", c->exc_task, (unsigned long)c->exc_pc);
    for (int i = 0; i < (int)c->exc_bt_info.depth && i < 12 && n < (int)sizeof b - 10; i++)
      n += snprintf(b + n, sizeof b - n, " %08lx", (unsigned long)c->exc_bt_info.bt[i]);
    lastCrash = b;
  }
  free(c);
}

// Board-level commands from the app or the CLI (who); everything else is an HID command for run().
static bool appHears;  // the app listens and shares what it heard

// The app's transcripts for the CLI, ~3 KB in PSRAM once the app shares any.
static heard_ring *heardRing() {
  static heard_ring *r;
  if (!r && (r = (heard_ring *)heap_caps_calloc(1, sizeof *r, MALLOC_CAP_SPIRAM))) heard_clear(r);
  return r;
}

String board(const String &line, const String &id, char who) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  int n = sscanf(arg.c_str(), "%ld %ld", &a, &b);
  if (who == FROM_CLI && !arb_allowed(ARB_CLI, ble_app_active()) && cmd != "crash") return APP_BUSY;
  if (cmd == "stream") {
    if (arg != "on" && arg != "off") return "err usage: stream on|off";
    video_stream(arg == "on", who);
    return "ok";
  }
  // The CLI must not change what the app relies on: the mirroring format, the radio, the pairing.
  if (who == FROM_CLI && (cmd == "app" || cmd == "log" || cmd == "unpair" || cmd == "res" || cmd == "fps" ||
                          cmd == "txpower" || cmd == "coex"))
    return "err not available from the CLI";
  if (cmd == "net" && who == FROM_CLI) {  // the board's Wi-Fi for the CLI's link (docs/cli-protocol.md, "Wi-Fi link")
    if (arg.length()) {
      if (!arg.startsWith("{")) return "err usage: net [{\"mode\":\"ap\"} | {\"mode\":\"sta\",\"ssid\":...,\"pass\":...}]";
      net_request(arg);  // applied by net_poll(): the reply goes out first
      return "ok";
    }
    return "ok " + net_json(false);
  }
  if (cmd == "app" || cmd == "log") {  // debug relay: Mac -> app ("app run ...") and app -> Mac ("log ...")
    ble_reply(cmd + " " + arg);         // every peer sees it; each side ignores lines it doesn't handle
    if (cmd == "log") Serial.println("log " + arg);  // and a Mac on USB serial
    return "ok";
  }
  // Phone audio for the CLI (docs/cli-protocol.md, "Phone audio"): the app shares what it heard, the CLI reads it
  // (`heard`) or asks for the raw audio to transcribe itself (`listen raw on`, Wi-Fi only).
  if (cmd == "hear" && who == FROM_APP) {
    appHears = arg == "on";
    if (!appHears && heardRing()) heard_clear(heardRing());
    return "ok";
  }
  if (cmd == "heard" && who == FROM_APP) {
    if (heardRing()) heard_add(heardRing(), millis(), arg.c_str(), arg.length());
    return "ok";
  }
  if (cmd == "heard") {
    static char out[HEARD_LINES * 64 + 64];
    int room = (int)cli_mtu() - 3 - CLI_OVERHEAD - 1 - 16;  // ATT, seal, kind byte, "<id> ok "
    static const heard_ring none = {};
    heard_json(heardRing() ? heardRing() : &none, millis(), appHears, out, room > 0 && room < (int)sizeof out ? room : sizeof out);
    return String("ok ") + out;
  }
  if (cmd == "listen" && who == FROM_CLI) {
    if (arg != "raw on" && arg != "raw off") return "err usage: listen raw on|off";
    if (arg == "raw on" && !cli_wifi()) return "err raw audio needs the Wi-Fi link (esp32agent net)";
    cli_audio(arg == "raw on");
    return "ok";
  }
  if (cmd == "logs") return board_log_page(arg);  // the RAM log, a page at a time (logbuf.h)
  if (cmd == "vdbg") return "ok " + video_debug();  // why a frozen screen stays frozen (video_link.cpp)
  if (cmd == "crash") return String("ok rst ") + resetReason() + ", last dump: " + lastCrash;
  if (cmd == "snap") { video_snap(id, who); return ""; }  // replies when the GOP is sent
  if (cmd == "bench") {
    if (n < 1 || a <= 0 || a > 4096) return "err usage: bench KB (1-4096)";
    video_bench(id, a, who);
    return "";
  }
  if (cmd == "res") {
    if (n < 2 || a < 160 || b < 160 || a > 1920 || b > 2560) return "err usage: res W H";
    cfg_set_res(a, b);
    airplay_set_display(a, b, cfg_fps());
    return "ok";
  }
  if (cmd == "fps") {
    if (n < 1 || a < 1 || a > 60) return "err usage: fps N (1-60)";
    cfg_set_fps(a);
    airplay_set_display(cfg_width(), cfg_height(), a);
    return "ok";
  }
  if (cmd == "txpower") {  // A/B knob in 0.25 dBm (8-84), not stored: every boot starts at 5 dBm
    if (n < 1 || a < 8 || a > 84 || esp_wifi_set_max_tx_power(a) != ESP_OK) return "err usage: txpower 8-84";
    return "ok";
  }
  if (cmd == "coex") {  // A/B knob, not stored: every boot starts at balance
    if (arg.length() && !net_coex(arg)) return "err usage: coex bt|balance|wifi";
    return String("ok ") + net_coex_mode();
  }
  if (cmd == "unpair") {
    ble_reply(id + " ok");  // answer before the link goes down
    ble_unpair();
    return "";
  }
  if (cmd == "hid") return ble_hid_command(arg.c_str()); // usb|ble|auto, pair, unpair; not a pointer report
  return drive(line, who);
}

String statusJson(bool full, char who) {
  static const char *apStates[] = {"idle", "connected", "streaming"};
  VideoStats v = video_stats(who);
  char s[1280];
  snprintf(s, sizeof s,
           "{\"fw\":\"%s\",\"up\":%lu,\"rst\":\"%s\",\"heap\":%u,\"psram\":%u,\"usb\":\"%s\",\"net\":\"%s\",\"ip\":\"%s\",\"sta\":%d,"
           "\"ap\":\"%s\",\"w\":%u,\"h\":%u,\"kbps\":%lu,\"fps\":%.1f,\"idr\":%lu,\"gop\":%lu,\"vage\":%lu,\"ovf\":%lu,\"fz\":%d,"
           "\"ble\":{\"mtu\":%u,\"phy\":%u,\"kbps\":%lu,\"drop\":%lu,\"rdrop\":%lu,\"n\":%d},"
           "\"udp\":{\"n\":%d,\"kbps\":%lu,\"nack\":%lu,\"drop\":%lu}}",
           FW_VERSION, millis(), resetReason(), (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), usbState(), net_mode(), net_ip().c_str(), net_peers(),
           apStates[airplay_state()], cfg_width(), cfg_height(), (unsigned long)v.kbps, v.fps, (unsigned long)v.idr_ms,
           (unsigned long)v.gop_bytes, (unsigned long)v.age_ms, (unsigned long)v.overflows, v.frozen, ble_mtu(), ble_phy(), (unsigned long)ble_kbps(), (unsigned long)ble_drops(),
           (unsigned long)ble_reply_drops(), ble_peers(), wv_active(), (unsigned long)wv_kbps(),
           (unsigned long)wv_nacks(), (unsigned long)wv_drops());
  // owner: who drives the phone (app, cli, none); the CLI's busy is 1 while that is the app
  bool app = ble_app_active();
  if (who == FROM_CLI) {  // the CLI's own link, not the app's (ble.mtu above is the app's)
    size_t l = strlen(s);
    snprintf(s + l - 1, sizeof s - l + 1, ",\"link\":{\"via\":\"%s\",\"mtu\":%u,\"kbps\":%lu}}", cli_wifi() ? "wifi" : "ble",
             cli_mtu(), (unsigned long)(cli_wifi() ? cli_tcp_kbps() : ble_kbps()));
  }
  const char *owner = arb_owner_name(arb_owner(app, cli_slot() >= 0));
  size_t len = strlen(s);
  snprintf(s + len - 1, sizeof s - len + 1, ",\"owner\":\"%s\"}", owner);
  // AirPlay audio: packets, sound segments, a segment going on, sender volume (dB), records lost to the app
  airplay_audio_stats au = airplay_audio();
  len = strlen(s);
  snprintf(s + len - 1, sizeof s - len + 1, ",\"aud\":{\"pk\":%lu,\"seg\":%lu,\"snd\":%d,\"vol\":%.0f,\"drop\":%lu,\"hear\":%d,\"raw\":%d}}",
           (unsigned long)au.packets, (unsigned long)au.segments, au.sound, au.volume, (unsigned long)wv_audio_drops(),
           appHears, cli_audio_wanted());
  if (who == FROM_CLI) {
    len = strlen(s);
    snprintf(s + len - 1, sizeof s - len + 1, ",\"busy\":%d}", app);
  }
  {
    char frag[180];
    hid_link_t facts = hidFacts();
    int prefNow = ble_hid_pref();
    int path = selectHidPath(prefNow, facts);
    hid_status_fragment(frag, sizeof frag, prefNow, path, &facts);
    len = strlen(s);
    snprintf(s + len - 1, sizeof s - len + 1, ",%s}", frag);
  }
  if (full) return s;
  // MTU below ~250 (before the MTU exchange): keep only the fields the app needs to stay alive
  snprintf(s, sizeof s, "{\"fw\":\"%s\",\"up\":%lu,\"rst\":\"%s\",\"usb\":\"%s\",\"net\":\"%s\",\"ap\":\"%s\",\"fz\":%d,\"ble\":{\"mtu\":%u}",
           FW_VERSION, millis(), resetReason(), usbState(), net_mode(), apStates[airplay_state()], v.frozen, ble_mtu());
  snprintf(s + strlen(s), sizeof s - strlen(s), ",\"owner\":\"%s\"", owner);
  if (who == FROM_CLI)
    snprintf(s + strlen(s), sizeof s - strlen(s), ",\"busy\":%d,\"ip\":\"%s\",\"link\":{\"via\":\"%s\",\"mtu\":%u}", app,
             net_ip().c_str(), cli_wifi() ? "wifi" : "ble", cli_mtu());
  {
    char frag[24];
    hid_link_t facts = hidFacts();
    hid_status_compact(frag, sizeof frag, selectHidPath(ble_hid_pref(), facts));
    snprintf(s + strlen(s), sizeof s - strlen(s), ",%s", frag);
  }
  strlcat(s, "}", sizeof s);
  return s;
}

void statusTask(void *) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    if (!ble_connected()) continue;
    String s = statusJson(true, FROM_APP);
    if (s.length() > ble_mtu() - 3u) s = statusJson(false, FROM_APP);
    ble_status(s);
    if (cli_slot() >= 0) ble_cli_status(statusJson(true, FROM_CLI), statusJson(false, FROM_CLI));
  }
}

// Board log lines to the app ("blog ...", into its debug.log) so a stall on a board plugged into the phone leaves
// evidence: the AirPlay log, and board_log (the video link's switches and resumes, which stayed in the 8 KB RAM log
// and were gone before anyone looked, 2026-11-06). At most 16 a second; the rest are counted and reported later.
void relayLine(const char *msg) {
  static uint32_t windowStart, sent, skipped;
  static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  uint32_t now = millis();
  bool send = false;
  uint32_t dropped = 0;
  portENTER_CRITICAL(&mux);
  if (now - windowStart >= 1000) { windowStart = now; sent = 0; }
  if (sent < 16) { sent++; send = true; dropped = skipped; skipped = 0; } else skipped++;
  portEXIT_CRITICAL(&mux);
  if (!send || !ble_connected()) return;
  String line = String("blog ") + now + " " + msg;
  if (dropped) line += String(" (+") + dropped + " skipped)";
  ble_reply(line);
}

// AirPlay log lines: to USB serial, the RAM log and the app.
void logLine(const char *msg) {
#if DEBUG_SERIAL
  Serial.printf("[%lu] %s\n", millis(), msg);
#endif
  board_log_keep(msg);
  relayLine(msg);
}

void startAirplay() {
  static airplay_cfg cfg;
  cfg.name = receiverName.c_str();
  esp_read_mac(cfg.hwaddr, ESP_MAC_WIFI_STA);
  cfg_ed25519_seed(cfg.ed25519_seed);
  cfg.width = cfg_width();
  cfg.height = cfg_height();
  cfg.max_fps = cfg_fps();
  cfg.on_config = video_on_config;
  cfg.on_au = video_on_au;
  cfg.on_audio = wv_audio;
  cfg.log = logLine;
  if (!airplay_start(&cfg)) board_log("airplay: start failed");
}

void setup() {
  Serial.begin(115200);
#if OTA_CRASH_TEST  // flash.sh --crash-test: an image that never comes up, to check the rollback after an update
  delay(3000);
  abort();
#endif
#if DEBUG_SERIAL
  for (int i = 0; i < 30 && !Serial; i++) delay(100);  // give a serial monitor 3 s to catch the boot lines
#endif
  mouse.begin();
  keyboard.begin();
  consumer.begin();
  USB.productName("HID Bridge");
  USB.onEvent(ARDUINO_USB_STOPPED_EVENT, usbHidStopped);
  USB.begin();
  esp_task_wdt_config_t wdt = {.timeout_ms = WATCHDOG_MS, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(NULL);
  readCrash();

  net_begin();
  if (!video_begin()) board_log("video: no PSRAM for the GOP buffer");
  if (!wv_begin()) board_log("video: Wi-Fi video channel failed to start");
  if (!ota_begin()) board_log("ota: failed to start");
  arb_init(&arb);
  cli_begin();
  if (!cli_tcp_begin()) board_log("cli: Wi-Fi link failed to start");
  ble_begin("ESP32 Agent " + board_suffix());
  startAirplay();
  // statusJson keeps a 1280-byte buffer on this task. 4096 was tight once the hid object was added.
  xTaskCreatePinnedToCore(statusTask, "status", 6144, NULL, 1, NULL, 1);
  board_log("boot %s fw %s: free internal heap %u (largest %u), PSRAM %u, reset %s\n", receiverName.c_str(), FW_VERSION,
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM), resetReason());
}

void loop() {
  static bool linked = false, cliLinked = false;
  esp_task_wdt_reset();
  net_poll();
  // Detect automatic route changes even while no HID command is arriving, and
  // retry failed releases once their original transport becomes usable again.
  activePath();
  bool cli = cli_slot() >= 0;
  // A gesture cut off mid-way must not leave the button held (long press); only the side that went holds it.
  bool app = ble_app_connected(), appActive = ble_app_active();
  if (linked && !app) {
    if (arb_gone(&arb, ARB_APP)) releaseHidReports(HELD_MOUSE);
    releaseHidReports(HELD_KEYS | HELD_CONSUMER);
    video_stream(false, FROM_APP);
    appHears = false;  // until the app says so again
  }
  if (cliLinked && !cli) {
    if (arb_gone(&arb, ARB_CLI)) releaseHidReports(HELD_MOUSE);
    video_stream(false, FROM_CLI);
  }
  // The app wins: a CLI session up alongside it stops at once and is dropped shortly after, once its status
  // (owner app, busy 1) and the `err busy` replies to anything it sent are out, so it doesn't load the radio.
  static uint32_t cliDropAt = 0;
  if (appActive && cli) {
    if (!cliDropAt) {
      if (arb_gone(&arb, ARB_CLI)) releaseHidReports(HELD_MOUSE);
      video_stream(false, FROM_CLI);
      ble_cli_status(statusJson(true, FROM_CLI), statusJson(false, FROM_CLI));
      cliDropAt = millis() + 300;
    } else if ((int32_t)(millis() - cliDropAt) >= 0) {
      cli_drop();
      cliDropAt = millis() + 2000;  // once more later if it's still there
    }
  } else {
    cliDropAt = 0;
  }
  linked = app;
  cliLinked = cli;
  // A freshly updated image is kept once it has run a while or the app got through; a crash or watchdog
  // reset before that makes the bootloader go back to the previous one.
  static bool confirmed = false;
  if (!confirmed && (linked || millis() > 30000)) { ota_confirm(); confirmed = true; }

  String line;
  // USB serial takes the same `<id> <command>` lines, for debugging from a Mac. Never wait for the rest of a line:
  // bytes without a newline (esptool's sync, say) held the loop until the watchdog reset the board.
  static String serialLine;
  bool serialDone = false;
  while (Serial.available() && !serialDone) {
    char ch = Serial.read();
    if (ch == '\n') serialDone = true;
    else if (serialLine.length() < 512) serialLine += ch;
  }
  if (serialDone) {
    line = serialLine;
    serialLine = "";
    line.remove(hid_line_length(line.c_str(), line.length()));
    if (line == "0 ble pair on") { ble_allow_pairing(60000); Serial.println("0 ok"); return; }
    if (line == "0 ble pair off") { ble_deny_pairing(); Serial.println("0 ok"); return; }
    if (line.startsWith("0 net ")) { net_request(line.substring(6)); Serial.println("0 ok"); return; }
    if (line == "0 netinfo") { Serial.println("0 " + net_json(true)); return; }  // what the app reads over BLE (incl. vtok)
    if (line == "0 status") { Serial.println("0 " + statusJson(true, FROM_APP)); return; }
    int sp = line.indexOf(' ');
    String id = sp < 0 ? line : line.substring(0, sp);
    Serial.println(id + " " + board(sp < 0 ? "" : line.substring(sp + 1), id, FROM_APP));
    return;
  }
  char who;
  if (!ble_pop_cmd(line, who, 50)) return;
  int sp = line.indexOf(' ');
  String id = sp < 0 ? line : line.substring(0, sp);
  String rest = sp < 0 ? "" : line.substring(sp + 1);
  rest.remove(hid_line_length(rest.c_str(), rest.length()));
#if DEBUG_SERIAL
  unsigned long started = millis();
#endif
  String reply = id.toInt() || id == "0" ? board(rest, id, who) : "err missing request id";
  if (reply.length()) ble_reply(id + " " + reply, who);
#if DEBUG_SERIAL
  Serial.printf("%s -> %s (%lu ms)\n", line.c_str(), reply.c_str(), millis() - started);
#endif
}
