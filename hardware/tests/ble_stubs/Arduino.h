#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
using String = std::string;
inline uint32_t clock_ms;
inline void (*delay_hook)();
inline uint32_t millis() { return clock_ms; }
inline void delay(unsigned ms) {
  clock_ms += ms;
  if (delay_hook) delay_hook();
}
