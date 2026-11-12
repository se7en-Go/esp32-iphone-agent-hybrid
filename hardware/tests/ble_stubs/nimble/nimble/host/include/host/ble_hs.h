#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>
#define MYNEWT_VAL(name) 320
#define BLE_HS_ENOMEM 6
#define BLE_HS_ENOENT 5
#define BLE_HS_EBUSY 15
struct ble_addr_t { uint8_t type; uint8_t val[6]; };
struct ble_store_key_sec { ble_addr_t peer_addr; uint8_t idx; };
struct ble_store_value_sec {};
int ble_store_read_our_sec(const ble_store_key_sec *, ble_store_value_sec *);
int ble_store_read_peer_sec(const ble_store_key_sec *, ble_store_value_sec *);
struct os_mbuf { std::vector<uint8_t> data; };
struct ble_gap_conn_desc {
  struct { bool encrypted; } sec_state;
  struct { uint8_t type; uint8_t val[6]; } peer_id_addr;
};
inline bool connected = true;
inline int ble_gap_conn_find(uint16_t h, ble_gap_conn_desc *d) {
  if (!connected || h != 1) return 1;
  *d = {};
  d->sec_state.encrypted = true;
  d->peer_id_addr.val[5] = (uint8_t)h;
  return 0;
}
inline int free_blocks = 64, allocation_failures, notify_failures, notify_rc;
inline unsigned allocations;
struct SentReport { uint16_t handle; std::vector<uint8_t> data; };
inline std::vector<SentReport> sent;
inline int os_msys_num_free() { return free_blocks; }
inline os_mbuf *ble_hs_mbuf_from_flat(const void *data, size_t n) {
  allocations++;
  if (allocation_failures > 0) { allocation_failures--; return nullptr; }
  free_blocks--;
  auto *bytes = static_cast<const uint8_t *>(data);
  return new os_mbuf{{bytes, bytes + n}};
}
// ESP32-S3 NimBLE 2.5.1 VHCI copies the packet and frees its mbuf before returning.
// NimBLE consumes the mbuf on error too.
inline int ble_gatts_notify_custom(uint16_t, uint16_t handle, os_mbuf *mbuf) {
  int rc = notify_rc;
  if (notify_failures > 0) { notify_failures--; rc = BLE_HS_ENOMEM; }
  if (!rc) sent.push_back({handle, mbuf->data});
  delete mbuf;
  free_blocks++;
  return rc;
}
