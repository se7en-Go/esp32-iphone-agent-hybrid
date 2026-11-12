# ESP32 iPhone Agent Hybrid

An ESP32-S3 N16R8, an iPhone, and a computer. Any MCP agent (Codex, Pi, Claude Code, Cursor, …) can click, swipe, and type on the real phone. The host runs natively on macOS and on Windows 10/11.

The COM port (USB-UART: CH343, FTDI, or CP210x) stays on the computer: flash, commands, the video token, and Wi-Fi stored in NVS. The USB port (GPIO19/20) is the iPhone's mouse and keyboard. Video is the board's own AirPlay receiver, TCP port 7201, decoded with PyAV. No UxPlay, GStreamer, or Bonjour install. Default network is STA on a 2.4 GHz LAN. The board's access point is only for a computer that also has ethernet, because that hotspot has no internet.

This tree has not been run on a real iPhone or a real board. See `docs/hardware-test.md`. iOS 16 and 17 mirroring to the on-board receiver is untested upstream and is the first hardware gate.

Firmware under `hardware/` is GPL-3.0 because it links the AirPlay port. The Python host is MIT. See `NOTICE`.

Quick start is in the Chinese `README.md` (`docs/setup-macos.md` and `docs/setup-windows.md`).
