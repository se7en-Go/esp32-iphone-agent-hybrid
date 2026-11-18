# ESP32 iPhone Agent Hybrid

[简体中文](README.md)

Control an iPhone from a computer using an ESP32-S3 N16R8 as a mouse and keyboard. Screen Mirroring sends the phone's picture to the computer. Use the [local web panel](http://127.0.0.1:8890/), MCP, or the CLI to observe the screen, click, swipe, and type.

## Quick start

1. Connect the board's **COM** port to the computer with a data cable. This connection stays in place when you use Bluetooth. The board's **USB** port is optional when using Bluetooth and serves as the wired mouse and keyboard connection to the iPhone.
2. Install Python 3.10 or later, set up the project, and configure the board's 2.4 GHz Wi‑Fi. Use the [macOS setup guide](docs/setup-macos.md) or the [Windows setup guide](docs/setup-windows.md).
3. On the iPhone, open **Settings → Accessibility → Touch → AssistiveTouch** and turn it on. For Bluetooth, start pairing with `scripts/hid-mode.sh pair` on macOS or the PowerShell equivalent on Windows. Select the device starting with **ESP32 Agent** in iPhone Bluetooth settings.
4. Start the server. Use `./scripts/run-local.sh start` on macOS or `scripts/run-server.ps1` on Windows. In iPhone Control Center, open **Screen Mirroring** and select **ESP32 Agent Mac** for a configured Mac receiver, or **ESP32 Agent** for the on-board receiver.
5. Open the local web panel. Wait for **device** and **screen** to turn green, then calibrate. Follow the on-screen pointer prompt if manual calibration is needed. Start by moving the pointer to the middle of the screen.

Before wiring the board's USB port to the iPhone, check that the **USB-OTG / IN-OUT** solder bridge is open to avoid backfeeding 5 V.

## Optional iPhone settings

- **Exact Chinese and multiline paste.** Set up the [clipboard Shortcut](docs/PASTE_INPUT.md) and assign it to **Long Press** under AssistiveTouch. The agent can trigger the shortcut through the ESP32, paste with `Cmd+V`, and verify the text. Handle the iOS paste permission prompt if it appears.
- **Middle-button Home.** With compatible BLE firmware, map **Button 3** to **Home** under the connected device's AssistiveTouch settings and verify the mapping using `middle-click-home enable`. See the [MCP and CLI guide](docs/MCP_CLI.md).
- **Full Keyboard Access.** Configure it only if you want to try a keyboard chord for the Shortcut. AssistiveTouch Long Press is the verified clipboard trigger.

macOS with the independent AirPlay receiver and ESP32 BLE has been tested on a real iPhone, including middle-button Home and confirmed Chinese paste after the iOS permission prompt. Windows automated checks pass. The full Windows physical-device flow and WeChat message sending still need device-level validation.

[Runtime and restart commands](docs/RUNTIME.md) · [MCP and CLI](docs/MCP_CLI.md) · [Troubleshooting](docs/troubleshooting.md) · [Project notices](NOTICE)
