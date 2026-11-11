// Shared serial/BLE command-line boundary. Keep payload processing out of the transport.
#pragma once
#include <stddef.h>

static inline size_t hid_line_length(const char *line, size_t len) {
  // CR/LF delimit records; spaces belong to the type payload, even if it is all spaces.
  while (len && (line[len - 1] == '\r' || line[len - 1] == '\n')) len--;
  return len;
}
