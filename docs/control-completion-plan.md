# iPhone control acceptance

The Mac receives the live AirPlay picture. The ESP32 sends Bluetooth mouse and keyboard input. A host using the MCP or CLI reads the current picture, acts, and checks the result. This route does not require the web agent's model.

| Area | Verified on the current iPhone | Remaining acceptance |
| --- | --- | --- |
| Mirror and startup | Repeated worker-only reloads kept the same live Mac receiver and retained calibration. | The Mac receiver cannot reliably distinguish a quick sender reconnect within the same UxPlay process. |
| Navigation | Returned to Home through observed AssistiveTouch clicks and through `iphone_home` from Freeform. Opened Settings, entered Wi-Fi, and returned to Settings. CLI Esc closed an iOS low-battery alert. The latest CLI Home call visibly returned from Safari to the Home screen. | That Safari call reported an unconfirmed outcome before a fresh Home frame arrived. Retest the new confirmation path on the iPhone. The customized keyboard Home shortcut did not navigate during its tests. |
| App selection | Opened Spotlight from Home and used its visible Freeform action to create a new board. Typed a Safari query and opened the selected app through the keyboard. Offline replay of two saved Spotlight frames now identifies the original search field. | Run `search-app` on the iPhone and verify the typed query and results. |
| Chinese input | Typed Pinyin, clicked the visible candidate, and checked that `你好` appeared in the Settings search field. The signed Shortcut template downloaded to the phone. | The separate staged Unicode clipboard route still needs Shortcut import, permissions, trigger setup, and true-phone paste. |
| Drawing | Enabled the Freeform pen and drew a circle, eyes and a smile. Finished pen mode with the result still visible on the new board. | Relative BLE motion is approximate. The screen provides feedback before press and after release. |
| Long press and double-tap | A CLI long press on Notes opened its Home screen quick-actions menu. The host opened a new test note from the menu and typed test text. | Verify word selection with a double-tap in the test note. |

The local web service and stdio MCP can be used independently. Hands-on navigation used MCP against the loopback server, with no model delegation. Mac mirror reload, device-session checks, and clipboard protocol checks are also covered by automated tests. Synthetic test runs do not verify iOS permission prompts, foreground shortcuts, or gesture effects on a particular app.
