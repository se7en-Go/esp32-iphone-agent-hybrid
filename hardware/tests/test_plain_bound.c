#include "src/cli/plain_bound.h"
#include <stdio.h>
#include <string.h>
#include <stdint.h>

int main(void) {
    uint8_t buf[83];
    const char *too_long =
        "pairing window closed: serial command '0 ble pair on', then pair within 60 seconds";
    size_t n = plain_payload_fit(80, strlen(too_long));
    memset(buf, 0xA5, sizeof buf);
    buf[0] = 1;
    memcpy(buf + 1, too_long, n);
    if (n + 1 > 80) return 1;
    if (buf[80] != 0xA5 || buf[81] != 0xA5 || buf[82] != 0xA5) return 2;
    if (strlen(too_long) <= 79) return 3; /* the old text must be the overflowing one */
    const char *fits = "closed: 0 ble pair on, 60s";
    if (plain_payload_fit(80, strlen(fits)) != strlen(fits)) return 4;
    if (sizeof "closed: 0 ble pair on, 60s" > 80) return 5;
    return 0;
}
