// Minimal host stand-ins for video_link.cpp; no scheduler or radio is started.
#pragma once
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
using std::min;
struct String : std::string {
  using std::string::string;
  String(const std::string &s) : std::string(s) {}
  explicit String(uint32_t n) : std::string(std::to_string(n)) {}
};
using QueueHandle_t = void *;
using TaskHandle_t = void *;
using SemaphoreHandle_t = void *;
constexpr int portMAX_DELAY = -1, pdTRUE = 1, pdPASS = 1;
inline int pdMS_TO_TICKS(int n) { return n; }
inline int xSemaphoreTake(SemaphoreHandle_t, int) { return 1; }
inline void xSemaphoreGive(SemaphoreHandle_t) {}
inline SemaphoreHandle_t xSemaphoreCreateMutex() { return nullptr; }
inline QueueHandle_t xQueueCreate(int, size_t) { return nullptr; }
inline int xQueueReceive(QueueHandle_t, void *, int) { return 0; }
inline int xQueueSend(QueueHandle_t, const void *, int) { return 0; }
inline int xTaskCreatePinnedToCore(void (*)(void *), const char *, int, void *, int, TaskHandle_t *, int) { return 0; }
inline void xTaskNotifyGive(TaskHandle_t) {}
inline void ulTaskNotifyTake(int, int) {}
uint32_t millis();
