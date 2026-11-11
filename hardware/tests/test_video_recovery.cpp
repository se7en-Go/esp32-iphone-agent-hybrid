// Exercise the real stream/snapshot code with a bounded GOP and fake transport.
#include <cassert>
#include <vector>
#include "../hid_bridge/video_link.cpp"

static uint32_t clockMs;
static bool resync;
static bool trimDuringReplay;
static std::vector<std::vector<uint8_t>> sent;
uint32_t millis() { return ++clockMs; }
bool ble_app_active() { return false; }
int cli_slot() { return -1; }
uint32_t cli_gen() { return 0; }
size_t cli_video_room() { return 0; }
bool cli_send_video(const uint8_t *, size_t) { return false; }
size_t ble_video_room() { return 0; }
bool ble_video_send(const uint8_t *, size_t) { return false; }
void ble_reply(const String &, char) {}
void board_log(const char *, ...) {}
int airplay_state() { return AIRPLAY_STREAMING; }
bool wv_active() { return true; }
bool wv_take_resync() { bool result = resync; resync = false; return result; }
bool wv_take_resume(uint32_t *) { return false; }  // plain hello, no decoder context
static void fillTail();
bool wv_send(const uint8_t *p, size_t n) {
  sent.emplace_back(p, p + n);
  if (trimDuringReplay && (p[2] & VF_CONFIG)) {
    trimDuringReplay = false;
    fillTail();  // AirPlay can keep appending while config/GOP is being sent.
  }
  return true;
}

static const uint8_t config[] = {0, 0, 1, 0x67, 0, 0, 1, 0x68};
static const uint8_t idr[] = {0, 0, 1, 0x65};
static const uint8_t predicted[] = {0, 0, 1, 0x41};
static uint8_t arena[8], tailArena[16], scratch[64];
static gop_au aus[2], tailAus[4];

static void fillTail() {
  for (int i = 0; i < 4; i++) video_on_au(predicted, sizeof predicted, false);
  assert(tailTrimmed);
}

static void reset() {
  sink = Sink{};
  sink.who = FROM_APP;
  sink.scratch = scratch;
  gop_init(&gop, arena, sizeof arena, aus, 2);
  gop_init(&tail, tailArena, sizeof tailArena, tailAus, 4);
  tailLive = tailTrimmed = false;
  sent.clear();
  resync = false;
  trimDuringReplay = false;
  video_on_config(config, sizeof config);
  video_on_au(idr, sizeof idr, true);
  video_on_au(predicted, sizeof predicted, false);
  streamStep(sink);  // established connection is following before the GOP freezes
  video_on_au(predicted, sizeof predicted, false);
  streamStep(sink);
  assert(gop.frozen && tailLive && sink.onTail);
  sent.clear();
}

static void test_frozen_gop_plain_hello_replays_config_idr_and_tail() {
  reset();
  resync = true;
  streamStep(sink);
  assert(sent.size() == 4 && "fresh hello must replay CONFIG, IDR, GOP P and tail P");
  assert(sent[0][2] & VF_CONFIG);
  assert(sent[1][2] & VF_IDR);
  for (const auto &chunk : sent) assert(!(chunk[2] & VF_RESUME));
  assert(sink.onTail && sink.tailNext == tail.n);
  sent.clear();
  video_on_au(predicted, sizeof predicted, false);
  streamStep(sink);
  assert(sent.size() == 1 && "the reconnected decoder keeps following live P frames");
  puts("ok frozen_gop_plain_hello_replays_config_idr_and_tail");
}

static void test_frozen_gop_rejects_incomplete_tail() {
  reset();
  fillTail();
  resync = true;
  streamStep(sink);
  assert(sent.empty() && "never replay P frames across a missing reference chain");
  video_on_au(idr, sizeof idr, true);
  streamStep(sink);
  assert(sent.size() == 2 && (sent[0][2] & VF_CONFIG) && (sent[1][2] & VF_IDR));
  puts("ok frozen_gop_rejects_incomplete_tail");
}

static void test_frozen_gop_trim_during_replay_stops_tail() {
  reset();
  resync = trimDuringReplay = true;
  streamStep(sink);
  assert(sent.size() == 3 && "config and old GOP are safe; a truncated tail is not");
  assert(!sink.onTail);
  sent.clear();
  streamStep(sink);
  assert(sent.empty());
  puts("ok frozen_gop_trim_during_replay_stops_tail");
}

static void test_new_idr_does_not_replay_previous_frozen_tail() {
  reset();
  resync = true;
  streamStep(sink);
  sent.clear();
  video_on_au(idr, sizeof idr, true);
  streamStep(sink);
  assert(sent.size() == 2 && "a new IDR must not be followed by the previous GOP's tail");
  assert((sent[0][2] & VF_CONFIG) && (sent[1][2] & VF_IDR));
  assert(!tailLive && !sink.onTail);
  puts("ok new_idr_does_not_replay_previous_frozen_tail");
}

int main() {
  test_frozen_gop_plain_hello_replays_config_idr_and_tail();
  test_frozen_gop_rejects_incomplete_tail();
  test_frozen_gop_trim_during_replay_stops_tail();
  test_new_idr_does_not_replay_previous_frozen_tail();
}
