#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "src/hid/command_line.h"

static void type_payload(char *line, char *typed) {
  line[hid_line_length(line, strlen(line))] = '\0';
  // Same two splits as loop() and run(): request id, then command, then literal payload.
  char *command = strchr(line, ' ');
  assert(command);
  char *payload = strchr(command + 1, ' ');
  assert(payload ? strncmp(command + 1, "type ", 5) == 0 : strcmp(command + 1, "type") == 0);
  strcat(typed, payload ? payload + 1 : "");
}

static void test_type_preserves_space_only_chunk(void) {
  char typed[502] = "", first[] = "1 type x\r\n", last[] = "3 type y\n";
  char middle[512] = "2 type ";
  memset(middle + 7, ' ', 499);
  strcpy(middle + 506, "\r\n");
  type_payload(first, typed);
  type_payload(middle, typed);
  type_payload(last, typed);
  assert(strlen(typed) == 501 && "x + 499 spaces + y must retain every payload byte");
  assert(typed[0] == 'x' && typed[500] == 'y');
  for (size_t i = 1; i < 500; i++) assert(typed[i] == ' ');
  puts("ok type_preserves_space_only_chunk");
}

static void test_type_preserves_payload_edges(void) {
  char typed[64] = "", line[] = "4 type  leading and trailing  \r\n";
  type_payload(line, typed);
  assert(strcmp(typed, " leading and trailing  ") == 0);
  assert(hid_line_length("0 status\r\n", 10) == 8);
  assert(hid_line_length("", 0) == 0);
  puts("ok type_preserves_payload_edges");
}

int main(void) {
  test_type_preserves_space_only_chunk();
  test_type_preserves_payload_edges();
}
