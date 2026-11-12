# Host unit tests for the firmware's pure-C parts. Usage: make -C hardware/tests
SRC = ../hid_bridge/src
CFLAGS = -std=c11 -Wall -Wextra -g -fsanitize=address,undefined

# mbedtls 3.x (4.x dropped the ECP API): Homebrew's mbedtls@3, an older Homebrew mbedtls, or the system one (apt libmbedtls-dev)
BREW := $(shell brew --prefix 2>/dev/null)
MBEDTLS := $(firstword $(wildcard $(BREW)/opt/mbedtls@3 $(BREW)/opt/mbedtls) /usr)

HOST_CFLAGS = -std=c11 -Wall -Wextra -g -fsanitize=address,undefined -I../hid_bridge

test: test_main.c $(SRC)/bplist/bplist.c $(SRC)/video/gop.c $(SRC)/airplay/keystream.c $(SRC)/cli/cli_proto.c $(SRC)/hid/arbiter.c $(SRC)/hid/pace.c $(SRC)/log/logring.c $(SRC)/airplay/mdns.c $(SRC)/airplay/audio.c $(SRC)/heard/heard.c plain_bound pair_window ble_ingress video_recovery command_line hid_transport hid_routes ble_hid
	$(CC) $(CFLAGS) -Istubs -I$(MBEDTLS)/include -o $@ $(filter %.c,$^) -L$(MBEDTLS)/lib -lmbedcrypto -lm
	./test
	./plain_bound
	./pair_window
	./ble_ingress
	./video_recovery
	./command_line
	./hid_transport
	./hid_routes
	./ble_hid

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

hid_transport: test_transport.c $(SRC)/hid/transport.c $(SRC)/hid/keys.c
	$(CC) $(HOST_CFLAGS) -o $@ test_transport.c $(SRC)/hid/transport.c $(SRC)/hid/keys.c

hid_routes.inc: ../hid_bridge/hid_bridge.ino extract_hid_routes.py
	python3 extract_hid_routes.py > $@

hid_routes: test_hid_routes.cpp hid_routes.inc route_stubs/Arduino.h $(SRC)/hid/transport.c $(SRC)/hid/keys.c $(SRC)/hid/pace.c
	$(CC) $(CFLAGS) -c $(SRC)/hid/transport.c -o route_transport.o
	$(CC) $(CFLAGS) -c $(SRC)/hid/keys.c -o route_keys.o
	$(CC) $(CFLAGS) -c $(SRC)/hid/pace.c -o route_pace.o
	$(CXX) -std=c++17 -Wall -Wextra -g -fsanitize=address,undefined -o $@ test_hid_routes.cpp route_transport.o route_keys.o route_pace.o

ble_hid: test_ble_hid.cpp ../hid_bridge/ble_hid.cpp $(SRC)/ble/advertising_gate.h $(wildcard ble_stubs/*.h ble_stubs/nimble/nimble/host/include/host/*.h) $(SRC)/hid/transport.c
	$(CXX) -std=c++17 -Wall -Wextra -g -fsanitize=address,undefined -Ible_stubs -o $@ test_ble_hid.cpp $(SRC)/hid/transport.c

sim_board:
	$(MAKE) -C sim cli_sim

clean:
	rm -f test plain_bound pair_window ble_ingress video_recovery video_gop.o command_line hid_transport hid_routes hid_routes.inc route_transport.o route_keys.o route_pace.o ble_hid
	$(MAKE) -C sim clean

.PHONY: clean sim_board
