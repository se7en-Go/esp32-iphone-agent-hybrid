#include "src/ble/pair_window.h"
#include <stdio.h>

int main(void) {
    int still = 1;
    uint32_t until = 1000;
    uint32_t expired = until + 70000;
    if (pair_window_still_open(1, until, expired, &still) != 0) return 1;
    if (still != 0) return 2; /* expiry must clear the armed flag */
    /* millis() wrapped so the signed delta looks open again; a cleared flag stays closed */
    uint32_t wrapped = until - 10;
    if (pair_window_still_open(still, until, wrapped, &still) != 0) return 3;
    if (still != 0) return 4;
    still = 99;
    if (pair_window_still_open(1, until, until - 1000, &still) != 1) return 5;
    if (still != 1) return 6;
    return 0;
}
