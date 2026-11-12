#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
class Preferences {
 public:
  uint8_t saved_count = 0;
  uint8_t saved_mode = 2;
  void begin(const char *, bool) {}
  uint8_t getUChar(const char *, uint8_t fallback) { return fallback; }
  void putUChar(const char *key, uint8_t value) {
    if (strcmp(key, "nb") == 0) saved_count = value;
    if (strcmp(key, "mode") == 0) saved_mode = value;
  }
  void putBytes(const char *, const void *, size_t) {}
  void getBytes(const char *, void *, size_t) {}
};
