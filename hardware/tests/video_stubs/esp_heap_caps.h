#pragma once
#include <cstdlib>
constexpr int MALLOC_CAP_SPIRAM = 0;
inline void *heap_caps_malloc(size_t size, int) { return malloc(size); }
inline void heap_caps_free(void *p) { free(p); }
