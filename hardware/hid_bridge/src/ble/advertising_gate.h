#pragma once
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// All application advertising starts and stops share this gate. Keep the pause
// flag set while deleting bonds, but release the mutex: unpair can trigger a
// disconnect callback which itself tries to advertise.
class BleAdvertisingGate {
  SemaphoreHandle_t mutex = nullptr;
  bool paused = false;
 public:
  void begin() { mutex = xSemaphoreCreateMutex(); }

  template <typename Advertise> void run(Advertise advertise) {
    xSemaphoreTake(mutex, portMAX_DELAY);
    if (!paused) advertise();
    xSemaphoreGive(mutex);
  }

  template <typename Stop> bool pause(Stop stop) {
    xSemaphoreTake(mutex, portMAX_DELAY);
    bool ok = false;
    if (!paused) {
      paused = true;
      ok = stop();
      if (!ok) paused = false;
    }
    xSemaphoreGive(mutex);
    return ok;
  }

  void resume() {
    xSemaphoreTake(mutex, portMAX_DELAY);
    paused = false;
    xSemaphoreGive(mutex);
  }
};
