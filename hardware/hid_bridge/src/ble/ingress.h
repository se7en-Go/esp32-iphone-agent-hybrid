#ifndef BLE_INGRESS_H
#define BLE_INGRESS_H
/* CLI writes use the upstream app-layer session (PAIR / HELLO / sealed DATA).
   They do not need an OS bond. Every other characteristic stays closed until
   the link is encrypted, so a stranger cannot dispatch HID or net commands. */
static inline int ble_write_reaches_handler(int is_cli, int link_encrypted) {
  return is_cli || link_encrypted;
}
#endif
