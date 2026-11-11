#ifndef PAIR_WINDOW_H
#define PAIR_WINDOW_H
#include <stdint.h>
/* Signed delta so a wrapped millis() cannot reopen an expired pairing window.
   On expiry, *still_armed is cleared. */
static inline int pair_window_still_open(int armed, uint32_t until, uint32_t now, int *still_armed) {
  if (!armed) {
    if (still_armed) *still_armed = 0;
    return 0;
  }
  if ((int32_t)(until - now) > 0) {
    if (still_armed) *still_armed = 1;
    return 1;
  }
  if (still_armed) *still_armed = 0;
  return 0;
}
#endif
