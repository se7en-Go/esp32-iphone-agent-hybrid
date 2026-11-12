#pragma once
#include "NimBLEDevice.h"
class NimBLEHIDDevice {
 NimBLECharacteristic chr[5]={NimBLECharacteristic(0),NimBLECharacteristic(1),NimBLECharacteristic(2),NimBLECharacteristic(3),NimBLECharacteristic(4)};
 public:
 NimBLEHIDDevice(NimBLEServer*) {}
 void setManufacturer(const char*) {}
 void setPnp(int,int,int,int) {}
 void setHidInfo(int,int) {}
 void setBatteryLevel(int) {}
 void setReportMap(uint8_t*,size_t) {}
 NimBLECharacteristic* getProtocolMode() {return &chr[0];}
 NimBLECharacteristic* getInputReport(int n) {return &chr[n];}
 NimBLECharacteristic* getOutputReport(int) {return &chr[4];}
};
