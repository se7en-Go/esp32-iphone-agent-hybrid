#include "keys.h"

#include <string.h>

static int named(const char *tok, int len, const char *lit) {
  int n = (int)strlen(lit);
  return len == n && memcmp(tok, lit, (size_t)n) == 0;
}

int hid_ascii(char c, uint8_t *usage, int *shift) {
  *shift = 0;
  if (c == '\0') return 0; // strchr would match a table's terminator.
  if (c == '\n' || c == '\r') {
    *usage = 0x28;
    return 1;
  }
  if (c >= 'a' && c <= 'z') {
    *usage = (uint8_t)(0x04 + (c - 'a'));
    return 1;
  }
  if (c >= 'A' && c <= 'Z') {
    *usage = (uint8_t)(0x04 + (c - 'A'));
    *shift = 1;
    return 1;
  }
  if (c >= '1' && c <= '9') {
    *usage = (uint8_t)(0x1E + (c - '1'));
    return 1;
  }
  if (c == '0') {
    *usage = 0x27;
    return 1;
  }
  // Unshifted, then the shifted symbol that shares the key.
  static const char plain[] = " -=[]\\;'`,./";
  static const char shifted[] = " _+{}|:\"~<>?";
  static const uint8_t usage_of[] = {0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38};
  const char *p = strchr(plain, c);
  if (p) {
    *usage = usage_of[p - plain];
    return 1;
  }
  p = strchr(shifted, c);
  if (p && p != shifted) { // shifted[0] is a placeholder so the columns line up
    *usage = usage_of[p - shifted];
    *shift = 1;
    return 1;
  }
  static const char digits[] = "1234567890";
  static const char bangs[] = "!@#$%^&*()";
  p = strchr(bangs, c);
  if (!p) return 0;
  c = digits[p - bangs];
  if (c == '0') *usage = 0x27;
  else *usage = (uint8_t)(0x1E + (c - '1'));
  *shift = 1;
  return 1;
}

int hid_key_token(const char *tok, int len, uint8_t *modifier, uint8_t *usage, uint16_t *consumer) {
  *modifier = 0;
  *usage = 0;
  *consumer = 0;
  if (len <= 0) return 0;
  if (len == 1) {
    int shift = 0;
    if (!hid_ascii(tok[0], usage, &shift)) return 0;
    if (shift) *modifier = 0x02;
    return 1;
  }
  if (named(tok, len, "enter")) *usage = 0x28;
  else if (named(tok, len, "esc")) *usage = 0x29;
  else if (named(tok, len, "tab")) *usage = 0x2B;
  else if (named(tok, len, "backspace")) *usage = 0x2A;
  else if (named(tok, len, "space")) *usage = 0x2C;
  else if (named(tok, len, "right")) *usage = 0x4F;
  else if (named(tok, len, "left")) *usage = 0x50;
  else if (named(tok, len, "down")) *usage = 0x51;
  else if (named(tok, len, "up")) *usage = 0x52;
  else if (named(tok, len, "ctrl")) *modifier = 0x01;
  else if (named(tok, len, "shift")) *modifier = 0x02;
  else if (named(tok, len, "alt")) *modifier = 0x04;
  else if (named(tok, len, "cmd")) *modifier = 0x08;
  else if (named(tok, len, "globe") || named(tok, len, "ime")) *consumer = 0x029D;
  else return 0;
  return 1;
}

int hid_fill_combo(const char *combo, uint8_t *modifier, uint8_t keys[6], int *nkeys, uint16_t *consumer) {
  *modifier = 0;
  *nkeys = 0;
  *consumer = 0;
  if (!combo || !*combo) return 0;
  const char *p = combo;
  while (*p) {
    while (*p == ' ') p++;
    const char *start = p;
    while (*p && *p != '+') p++;
    int len = (int)(p - start);
    while (len > 0 && (start[len - 1] == ' ' || start[len - 1] == '\t')) len--;
    if (len <= 0) return 0;
    uint8_t mod = 0, usage = 0;
    uint16_t cons = 0;
    if (!hid_key_token(start, len, &mod, &usage, &cons)) return 0;
    if (cons) {
      if (*consumer || *nkeys || *modifier) return 0;
      *consumer = cons;
    } else if (usage) {
      if (*consumer || *nkeys >= 6) return 0;
      if (mod) *modifier = (uint8_t)(*modifier | mod);
      keys[(*nkeys)++] = usage;
    } else if (mod) {
      if (*consumer) return 0;
      *modifier = (uint8_t)(*modifier | mod);
    } else {
      return 0;
    }
    if (*p == '+') p++;
  }
  return *consumer || *nkeys || *modifier;
}
