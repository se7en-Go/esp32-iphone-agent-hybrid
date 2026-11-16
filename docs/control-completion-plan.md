# iPhone control acceptance

The Mac receives the live AirPlay picture. The ESP32 sends Bluetooth mouse and keyboard input. A host using the MCP or CLI reads the current picture, acts, and checks the result. This route does not require the web agent's model.

| Area | Verified on the current iPhone | Remaining acceptance |
| --- | --- | --- |
| Mirror and startup | Repeated worker-only reloads kept the same live Mac receiver and retained calibration. The framed receiver passed a synthetic two-session GStreamer decode and same-process Unix socket test. It was activated while the mirror was disconnected after a Wi-Fi change. The iPhone then reconnected, and MCP reported fresh live frames and the existing calibration. | Verify a rapid iPhone disconnect and reconnect on the same framed receiver process, with the old observation invalidated. |
| Navigation | Returned to Home through observed AssistiveTouch clicks and through `iphone_home` from Freeform. Opened Settings, entered Wi-Fi, and returned to Settings. CLI Esc closed an iOS low-battery alert. A verified click in Safari's open AssistiveTouch menu visibly returned to Home. | The Safari call reported an unconfirmed outcome before a fresh Home frame arrived. Automated tests cover late confirmation both when the menu was already open and when the action opened it. A confirmed late result still needs a device test. The customized keyboard Home shortcut did not navigate during its tests. |
| App selection | Opened Spotlight from Home and used its visible Freeform action to create a new board. The automatic Spotlight action entered Safari and returned the live results. A separate verified click on the top app result opened Safari. | Retest the full sequence from another app. Automated tests verify that search can continue from the same acknowledged Home click after a late frame, and that another input before the search key stops it. |
| Chinese input | Typed Pinyin and verified `你好` in Settings. The corrected Shortcut ran through a stationary AssistiveTouch hold while Safari kept the test field focused. Two Safari tests independently reported exact equality, including `00123` and `00042`. A third Safari paste returned `completed` after accepting the iOS paste prompt over ESP32 Bluetooth. A staged paste in the native Notes test note reached the iOS permission sheet, and the intended Chinese text, newlines, `00084` and `AbC xYz` were visible after accepting it over ESP32. | The Notes confirmation request expired after a fresh-screen rejection, so the native test verified the visible paste but did not return `completed`. Verify that confirmation and the keyboard trigger. |
| Drawing | Enabled the Freeform pen and drew a circle, eyes and a smile. Finished pen mode with the result still visible on the new board. | Relative BLE motion is approximate. The screen provides feedback before press and after release. |
| Long press and double-tap | A CLI long press on Notes opened its Home screen quick-actions menu. The host opened a test note and typed test text. After requiring a visually measured landing, Safari with default gesture settings recorded two trusted clicks and one native double-click on the attached iPhone. | Confirm word selection in Notes. The Safari fixture's alternate touch settings emitted two separate clicks even at a shorter interval. |

The local web service and stdio MCP can be used independently. Hands-on navigation used MCP against the loopback server, with no model delegation. Mac mirror reload, device-session checks, and clipboard protocol checks are also covered by automated tests. Synthetic test runs do not verify iOS permission prompts, foreground shortcuts, or gesture effects on a particular app.

## 2026-11-16 acceptance pass — branch `muse/iphone-control-acceptance`

Baseline: `codex/complete-iphone-control` at `83d4af7`. All work below is unmerged; merge only after Codex review and CI pass.

### Automated test results

- Full Python suite: **2199 passed, 1 skipped** (2026-11-16, after the item 2 and item 4 fixes).
- Native UxPlay GStreamer test (720p burst, 30 distinct frames): old patch **0/30 JPEGs**, new patch **30/30**.
- New: `server/tests/test_home_occluded_return.py` — 6 tests (2 accept, 4 refuse), all passing.
- New: `server/tests/test_confirm_sheet_retry.py` — 2 tests, both passing; `test_staged_clipboard.py` / `test_paste_confirmation.py` — 65 tests, all passing.

### Item 1 — web mirror stale/stalling frames

- Root cause: the pre-decode `queue` in the framed GStreamer pipeline was `max-size-buffers=2 leaky=downstream`, so under decode lag it dropped the IDR carrying SPS/PPS and `h264parse` had no `config-interval`; the decoder stalled until the phone's next IDR. Fix: `max-size-buffers=16 leaky=no` + `h264parse config-interval=-1` (`native/uxplay/patches/uxplay-1.73.7-hafe-v1.patch`, `upstream.json` re-pinned). Deployed receiver `6acb30860f144578988ddf6431c111f7` (pid 69934), generation 2 → 4, calibration preserved.
- **Still unverified on device:** the iPhone does not auto-reconnect after a receiver restart — the user must re-select "ESP32 Agent Mac" in Control Center → Screen Mirroring. Then verify frames update continuously, a rapid disconnect+reconnect yields new frames with the old ticket rejected, and a worker-only restart keeps the same receiver instance and a usable mirror.

### Item 2 — Home → Spotlight → target app

- Root cause: on the Home icon the pointer disc is occluded to a crescent, so the landing measurement failed before any click was sent (`PointerLandingError`). Fix (scheme A, `server/hidagent/phone.py`): accept occluded-return evidence only for the Home-menu case — a pointer-sized grey disc seen at the probe's escape point in an outward frame, absent there in the baseline frame, with no pointer-sized disc on the return path and no change outside the icon box. Gated on the navigation context (action `home`, kind `menu`). Disc colour uses the same mid-grey range as the existing landing check, so dark interiors of ring-shaped UI are not mistaken for the pointer.
- **Still unverified on device:** Home visible → Spotlight → target app opens end-to-end, and retest the web "主屏幕" entry.

### Item 3 — native Notes double-tap word selection

- No code change. No selection/highlight/handle detector exists; acceptance is by screenshot (highlight + both handles).
- **Still unverified on device:** double-tap a word in Notes and confirm the selection appears.

### Item 4 — native Notes Chinese staged paste to `completed`

- Config: `HIDAGENT_CLIPBOARD_TTL=180`, `HIDAGENT_CLIPBOARD_TRIGGER_KEY=ctrl+alt+p` (`server/.local-data/runtime.env`, local only); capabilities now report `trigger_configured: true`.
- Code fix (`server/hidagent/clipboard_control.py`): when the guard rejects and `verify_after_paste_permission` fails with `first_post_paste_frame`, retry with the most recent frames that could still show the permission sheet (newest first, up to 4 candidates). The staged-clipboard test's verify fake was updated to allow the extra calls.
- **Still unverified on device:** a staged paste with new test text (different Chinese words, newline positions, leading-zero number) returning `completed`, and the independent trigger-key check (prepare → `awaiting_copy` → send only `ctrl+alt+p` → `awaiting_paste`, `copied=true`). The user must bind Control+Option+P to `ESP32 Agent Clipboard` in Settings → Accessibility → Keyboard & Typing → Full Keyboard Access → Commands.
