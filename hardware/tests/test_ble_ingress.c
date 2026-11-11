#include "src/ble/ingress.h"
#include <stdio.h>

int main(void) {
    /* Handsy CLI: app-layer crypto, no OS bond. The frame must still reach cli_on_frame. */
    if (!ble_write_reaches_handler(1, 0)) return 1;
    /* A stranger on the HID/net characteristics still cannot dispatch. */
    if (ble_write_reaches_handler(0, 0)) return 2;
    if (!ble_write_reaches_handler(0, 1)) return 3;
    if (!ble_write_reaches_handler(1, 1)) return 4;
    return 0;
}
