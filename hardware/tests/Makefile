# Host unit tests for the firmware's pure-C parts. Usage: make -C hardware/tests
SRC = ../hid_bridge/src
CFLAGS = -std=c11 -Wall -Wextra -g -fsanitize=address,undefined

# mbedtls 3.x (4.x dropped the ECP API): Homebrew's mbedtls@3, an older Homebrew mbedtls, or the system one (apt libmbedtls-dev)
BREW := $(shell brew --prefix 2>/dev/null)
MBEDTLS := $(firstword $(wildcard $(BREW)/opt/mbedtls@3 $(BREW)/opt/mbedtls) /usr)

HOST_CFLAGS = -std=c11 -Wall -Wextra -g -fsanitize=address,undefined -I../hid_bridge

test: test_main.c $(SRC)/bplist/bplist.c $(SRC)/video/gop.c $(SRC)/airplay/keystream.c $(SRC)/cli/cli_proto.c $(SRC)/hid/arbiter.c $(SRC)/hid/pace.c $(SRC)/log/logring.c $(SRC)/airplay/mdns.c $(SRC)/airplay/audio.c $(SRC)/heard/heard.c plain_bound pair_window ble_ingress video_recovery command_line
	$(CC) $(CFLAGS) -Istubs -I$(MBEDTLS)/include -o $@ $(filter %.c,$^) -L$(MBEDTLS)/lib -lmbedcrypto -lm
	./test
	./plain_bound
	./pair_window
	./ble_ingress
	./video_recovery
	./command_line

plain_bound: test_plain_bound.c
	$(CC) $(HOST_CFLAGS) -o $@ $<

pair_window: test_pair_window.c
	$(CC) $(HOST_CFLAGS) -o $@ $<

ble_ingress: test_ble_ingress.c
	$(CC) $(HOST_CFLAGS) -o $@ $<

video_recovery: test_video_recovery.cpp ../hid_bridge/video_link.cpp $(SRC)/video/gop.c video_stubs/Arduino.h video_stubs/esp_heap_caps.h
	$(CC) $(CFLAGS) -c $(SRC)/video/gop.c -o video_gop.o
	$(CXX) -std=c++17 -Wall -Wextra -Wno-missing-field-initializers -g -fsanitize=address,undefined -Ivideo_stubs -o $@ $< video_gop.o

command_line: test_command_line.c $(SRC)/hid/command_line.h
	$(CC) $(HOST_CFLAGS) -o $@ $<

clean:
	rm -f test plain_bound pair_window ble_ingress video_recovery video_gop.o command_line

.PHONY: clean
