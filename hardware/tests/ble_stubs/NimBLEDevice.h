#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>
class NimBLEAddress {
  uint8_t type_, val_[6];
 public:
  NimBLEAddress(const uint8_t *val, uint8_t type) : type_(type) { memcpy(val_, val, 6); }
  uint8_t getType() const { return type_; }
  const uint8_t *getVal() const { return val_; }
};
class NimBLEConnInfo {
  uint16_t handle_;
 public:
  explicit NimBLEConnInfo(uint16_t handle) : handle_(handle) {}
  uint16_t getConnHandle() { return handle_; }
  bool isEncrypted() { return true; }
  NimBLEAddress getIdAddress() {
    uint8_t val[6] = {0, 0, 0, 0, 0, (uint8_t)handle_};
    return NimBLEAddress(val, 0);
  }
};
class NimBLECharacteristic;
class NimBLECharacteristicCallbacks {
 public:
  virtual ~NimBLECharacteristicCallbacks() = default;
  virtual void onSubscribe(NimBLECharacteristic *, NimBLEConnInfo &, uint16_t) {}
};
class NimBLECharacteristic {
  uint16_t handle_;
 public:
  NimBLECharacteristicCallbacks *cb = nullptr;
  explicit NimBLECharacteristic(uint16_t h = 0) : handle_(h) {}
  void setValue(const uint8_t *, size_t) {}
  uint16_t getHandle() { return handle_; }
  void setCallbacks(NimBLECharacteristicCallbacks *c) { cb = c; }
};
class NimBLEServer {
 public:
  void disconnect(uint16_t) {}
};
bool test_delete_bond(const NimBLEAddress &);
class NimBLEDevice {
 public:
  static NimBLEServer *getServer() { static NimBLEServer s; return &s; }
  static bool deleteBond(const NimBLEAddress &a) { return test_delete_bond(a); }
};
