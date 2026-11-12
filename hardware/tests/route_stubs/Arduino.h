#pragma once
#include <cstdint>
#include <cstring>
#include <string>

// The String operations used by the sketch's HID handlers.
class String : public std::string {
 public:
  using std::string::string;
  String(const std::string &s) : std::string(s) {}
  int indexOf(char c) const {
    size_t i = find(c);
    return i == npos ? -1 : static_cast<int>(i);
  }
  String substring(size_t start, size_t end = npos) const {
    return substr(start, end == npos ? npos : end - start);
  }
};
inline String operator+(const String &s, int value) { return static_cast<const std::string &>(s) + std::to_string(value); }
