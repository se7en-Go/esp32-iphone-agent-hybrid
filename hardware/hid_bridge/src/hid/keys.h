#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// US keyboard usage for one printable ASCII byte. *shift is 1 when the HID
// shift modifier has to be down. Returns 0 for bytes this keyboard cannot send.
int hid_ascii(char c, uint8_t *usage, int *shift);

// One combo token (no '+'): a name the USB path already accepts, a single
// character, or the globe/ime consumer key (usage 0x01AE, AL Keyboard Layout).
// On success exactly one of *modifier, *usage, *consumer is non-zero.
int hid_key_token(const char *tok, int len, uint8_t *modifier, uint8_t *usage, uint16_t *consumer);

// "cmd+h", "ctrl+space", "globe". Consumer combos cannot mix with keys.
// keys[] receives up to 6 usages. Returns 0 when a token is unknown.
int hid_fill_combo(const char *combo, uint8_t *modifier, uint8_t keys[6], int *nkeys, uint16_t *consumer);

#ifdef __cplusplus
}
#endif
