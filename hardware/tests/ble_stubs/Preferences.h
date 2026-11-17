#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <map>
#include <string>
#include <vector>
class Preferences {
 public:
  uint8_t saved_count = 0;
  uint8_t saved_mode = 2;
  std::map<std::string, std::vector<uint8_t>> blobs;
  void begin(const char *, bool) {}
  void clear() {
    saved_count = 0;
    saved_mode = 2;
    blobs.clear();
  }
  uint8_t getUChar(const char *key, uint8_t fallback) {
    if (strcmp(key, "nb") == 0) return saved_count;
    if (strcmp(key, "mode") == 0) return saved_mode;
    return fallback;
  }
  void putUChar(const char *key, uint8_t value) {
    if (strcmp(key, "nb") == 0) saved_count = value;
    if (strcmp(key, "mode") == 0) saved_mode = value;
  }
  void putBytes(const char *key, const void *data, size_t len) {
    const auto *p = static_cast<const uint8_t *>(data);
    blobs[key] = std::vector<uint8_t>(p, p + len);
  }
  size_t getBytes(const char *key, void *data, size_t max_len) {
    auto it = blobs.find(key);
    if (it == blobs.end()) return 0;
    size_t n = std::min(max_len, it->second.size());
    memcpy(data, it->second.data(), n);
    return n;
  }
};
