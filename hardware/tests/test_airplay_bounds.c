// Host ASan/UBSan tests for bplist integer-overflow guards and FairPlay mode bounds.
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "../hid_bridge/src/bplist/bplist.h"
#include "../hid_bridge/src/airplay/ap_internal.h"

static void test_bplist_overflows(void) {
  bp_reader r;
  uint8_t buf[64];
  memset(buf, 0, sizeof buf);
  memcpy(buf, "bplist00", 8);
  uint8_t *t = buf + sizeof buf - 32;
  t[6] = 1; t[7] = 1;
  t[15] = 1;
  memset(t + 24, 0xff, 8);  // table = UINT64_MAX
  assert(bp_open(&r, buf, sizeof buf) < 0);

  // table valid, dict count = UINT64_MAX -> must not loop or read OOB.
  memset(buf, 0, sizeof buf);
  memcpy(buf, "bplist00", 8);
  buf[8] = 0xDF;
  buf[9] = 0x13;
  memset(buf + 10, 0xff, 8);
  buf[18] = 8;
  t = buf + sizeof buf - 32;
  t[6] = 1; t[7] = 1;
  t[15] = 1;
  t[31] = 18;
  int top = bp_open(&r, buf, sizeof buf);
  assert(top == 0);
  assert(bp_count(&r, top) == 0);
  assert(bp_dict_get(&r, top, "ekey") == -1);

  // data length = UINT64_MAX -> must fail without pointer wrap.
  buf[8] = 0x4F;
  const uint8_t *out = NULL;
  size_t olen = 0;
  assert(!bp_get_data(&r, top, &out, &olen));

  // UTF-16 string count = 1ULL<<63 -> n*2 wraps to 0 if unchecked.
  buf[8] = 0x6F;
  memset(buf + 10, 0, 8);
  buf[10] = 0x80;
  char str[32];
  assert(!bp_get_string(&r, top, str, sizeof str));
  puts("ok bplist_overflows");
}

static void test_fairplay_invalid_mode_rejected(void) {
  uint8_t req[164], keymsg[164], res[32], cipher[72], out[16];
  memset(req, 0, sizeof req);
  memset(cipher, 0x42, sizeof cipher);
  req[4] = 0x03;
  req[12] = 200;  // invalid mode (> 3)
  assert(fairplay_handshake(keymsg, req, res) == -1);

  // Defense-in-depth: direct decrypt call with invalid mode must not OOB-read tables.
  memset(keymsg, 0, sizeof keymsg);
  keymsg[4] = 0x03;
  keymsg[12] = 200;
  memset(out, 0xAA, sizeof out);
  fairplay_decrypt(keymsg, cipher, out);
  uint8_t zero[16] = {0};
  assert(memcmp(out, zero, 16) == 0);

  // Valid mode (0..3) still succeeds cleanly.
  req[12] = 3;
  assert(fairplay_handshake(keymsg, req, res) == 0);
  fairplay_decrypt(keymsg, cipher, out);
  puts("ok fairplay_invalid_mode_rejected");
}

int main(void) {
  test_bplist_overflows();
  test_fairplay_invalid_mode_rejected();
  return 0;
}
