# iPhone control acceptance

The Mac receives the live AirPlay picture. The ESP32 sends Bluetooth mouse and keyboard input. A host using the MCP or CLI reads the current picture, acts, and checks the result. This route does not require the web agent's model.

| Area | Verified on the current iPhone | Remaining acceptance |
| --- | --- | --- |
| Mirror and startup | Repeated worker-only reloads kept the same live Mac receiver and retained calibration. | The Mac receiver cannot reliably distinguish a quick sender reconnect within the same UxPlay process. |
| Navigation | Returned to Home through observed AssistiveTouch clicks and, later, through `iphone_home` from Freeform. Opened Settings, entered Wi-Fi, and returned to Settings. | Some pointer landings still required another observation. The customized keyboard Home shortcut did not navigate during its tests. |
| App selection | Opened Spotlight from Home and used its visible Freeform action to create a new board. Typed a Safari query and opened the selected app through the keyboard. | Automatic Spotlight query entry rejected the localized search field in this run. |
| Chinese input | Typed Pinyin, clicked the visible candidate, and checked that `你好` appeared in the Settings search field. The signed Shortcut template downloaded to the phone. | The separate staged Unicode clipboard route still needs Shortcut import, permissions, trigger setup, and true-phone paste. |
| Drawing | Enabled the Freeform pen and drew a circle, eyes and a smile. Finished pen mode with the result still visible on the new board. | Relative BLE motion is approximate. The screen provides feedback before press and after release. |

The local web service and stdio MCP can be used independently. Hands-on navigation used MCP against the loopback server, with no model delegation. Mac mirror reload, device-session checks, and clipboard protocol checks are also covered by automated tests. Synthetic test runs do not verify iOS permission prompts, foreground shortcuts, or gesture effects on a particular app.
