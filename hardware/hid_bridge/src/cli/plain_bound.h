#ifndef PLAIN_BOUND_H
#define PLAIN_BOUND_H
#include <stddef.h>
/* CLI notify buffer is 80 bytes: 1 type byte plus at most 79 of payload. */
static inline size_t plain_payload_fit(size_t cap, size_t n) {
  if (cap < 1) return 0;
  if (n > cap - 1) return cap - 1;
  return n;
}
#endif
