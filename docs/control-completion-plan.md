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

## 2026-11-16 acceptance pass, round 2 — branch `muse/iphone-control-acceptance`

Commits on top of round 1 (`ed4445e`): `3096228` (item 2), `e757651` (item 1), `8ad6b04` (item 4). Merge still waits for Codex review and CI pass.

### Automated vs real-device results

- **Automated:** full Python suite after `e757651`: **2207 passed, 1 skipped**. Full suite after `8ad6b04`: **2210 passed, 1 skipped** (2026-11-16, 312 s); the 70 affected tests (paste-confirmation + confirm-sheet-retry + staged-clipboard, including the Safari regression fixtures) pass within it.
- **Real-device:** **not yet run** — blocked on the user re-selecting "ESP32 Agent Mac" in Control Center → Screen Mirroring after the HAFE/2 receiver restart (new receiver `1a0a4b77…`, pid 24772, generation 4, video `waiting`). Device, HID BLE conn=1/bond=1 and calibration are all still true.

### Item 1 — mirror backlog age + overload recovery (supersedes the round-1 queue fix)

- Native HAFE v2: `sample()` reads `GST_BUFFER_PTS(buffer)` + pipeline base time into a monotonic-ms capture time, written into header bytes 28–31 (the old `reserved` field); header version 1 → 2; `--hafe-protocol` prints `HAFE/2`. On appsrc overflow the pipeline is torn down and a new epoch begun (new BEGIN; `session->epoch` no longer const) instead of freezing for the rest of the AirPlay session. Python: the parser accepts v1 (`capture_ms=None`) and v2; `_on_native_record` drops JPEGs with capture age > 2.0 s or < −0.5 s; the age is kept in `_last_capture_age` and reported as `replay_lag_s` in `preview()`; `board_ts`/`frame_ts` are deliberately not set from the capture time. `patch_sha256` re-pinned to `61bfe075…` in `upstream.json`. Native tests: `test_hafe.cpp` updated for v2; `test_gstreamer.cpp` overflow test feeds bursts by total bytes up to 48 MB, expects END(old epoch) + BEGIN(new epoch), then JPEGs on the newest epoch, all v2 with nonzero, non-decreasing capture timestamps. Build verified: the candidate binary prints `HAFE/2`; build-manifest checks `['capability probe', 'test_hafe', 'test_gstreamer']`.
- Patch lesson: `git apply` trusts `@@` hunk counts; edited hunks with stale counts silently truncate files (here `hafe_stream.hpp` lost its last 3 lines). Fixed by recomputing every hunk count (renderer 101→128, stream.cpp 83→85, stream.hpp 23→26).
- Deployed receiver: `server/.local-data/runtime.env` repointed to `uxplay-hafe-cdbfba8-managed`; new receiver instance `1a0a4b7784a64f87bec20ad2f18dde29`, pid 24772, UxPlay child pid 24774.
- **Still unverified on device:** continuous frames with `frame_age_s` < 5 s and small `replay_lag_s`; rapid disconnect+reconnect → new frames, old `screen_id` rejected, calibration kept; worker-only restart keeps the same receiver instance and a live mirror.

### Item 2 — Home misclick (supersedes round-1 scheme A; scheme A removed)

- Real sample `/private/tmp/iphone-control-current/notes-home-probe-redacted.zip` (frames 00–35): the real iOS pointer is dark grey (46,44,47), ~25 px, visible in frame 20 near (0.606, 0.790), absent from return frames 30/35. Icon-box grey counts overlap across phases, so icon-box evidence is unusable. Round-1 scheme A accepted from *absence* of evidence and cannot be made safe — deleted entirely (`_pointer_discs_near`, `_occluded_home_outward`, `_occluded_home_accept`). A return must now pair a disc displaced by the return move; otherwise the landing stays unconfirmed and `tap_verified` raises `PointerLandingError` and sends zero clicks. Pointer-colour filter widened 90 → 40 in `_observe_landing` and `text_target.py` (2 sites) so the dark pointer is detectable. `_mask_pointer_discs` in `screen_compare.py` stays 100–200 — widening it masked an opaque dark disc that is real content (broke `test_pointer_fade_antialias`).
- New tests: `test_home_probe_sample.py` with real fixtures in `server/tests/fixtures/home_probe/` (frames 00, 20, 30, 35); `test_sample_return_sends_zero_clicks` — `tap_verified` raises, no click/press sent. `test_home_occluded_return.py` rewritten: 2 accept tests deleted, 4 refuse tests kept.
- **Still unverified on device:** web "主屏幕" retest; Home → Spotlight → specified app actually opens, recorded end-to-end.

### Item 3 — native Notes double-tap word selection (unchanged, device-only)

- **Still unverified on device:** double-tap a word in the native Notes test note; keep a screenshot with the highlight and both selection handles visible; classify any failure (landing interception / send failure / iOS received it but no selection).

### Item 4 — native Notes Chinese staged paste to `completed` (supersedes the round-1 retry)

- `confirm()` fallback rewritten: candidates only from `controls.screen.recent_frames` with matching generation and `updated` after this transaction's `completed_at` (Cmd+V) and at or before the result observation's `updated`; the receipt gains `sheet_frame_updated` + `sheet_frame_generation` and `confirmation_evidence="paste_permission_dismissed"`. A stale pre-paste sheet is refused; a sheet buried under later ordinary frames is found. `paste_confirmation.py`: append support — `current_lines` must end with the expected lines and the preceding lines must equal `original_lines`; `_same_field_outline` skips sides with no visible border (edge-profile max < 22), so a fully borderless field passes the outline check. Untouched: the observed-text SHA-256 check, the `paste_sent` precondition, the session lock, the three-frame permission verification.
- Tests: 70 passed (paste-confirmation + confirm-sheet-retry + staged-clipboard, including the Safari regression fixtures).
- **Still unverified on device:** Notes paste with new text (different Chinese words, newline positions, leading-zero number, mixed case; the already-verified set 你好00123 / 00123 / 00042 / 00084 / AbC xYz must not be reused); verify the actual Notes content against the `completed` receipt; trigger-key check: when configured, `prepare` auto-sends the key once, then only poll `copied=true`. The user must confirm Control+Option+P is bound to `ESP32 Agent Clipboard` in Settings → Accessibility → Keyboard & Typing → Full Keyboard Access → Commands.

## 2026-11-16 acceptance pass, round 3 — branch `muse/iphone-control-acceptance`

Commits on top of round 2 (`6681954`): `d69e30b` (both regressions below). Merge still waits for Codex review and CI pass.

### Automated vs real-device results

- **Automated:** full Python suite after `d69e30b`: **2217 passed, 1 skipped** (2026-11-16, 313 s); the 83 tests in the files touched by the two regressions pass within it.
- **Real-device:** partial — item 1a/1b passed; items 2/3 failed with zero clicks sent (diagnosis below, needs a detection-code fix decision); item 4 blocked on the iPhone-side shortcut binding (see below); item 1c not yet run.

### Regression 1 — mirror time validation (Codex offline repro)

- `FrameSnapshot` gained `capture_age_s` (seconds between iPhone capture and arrival; None = unknown: board path, legacy UxPlay, HAFE/1). `fresh_frame(since=...)` now requires `frame.updated - frame.capture_age_s > since` (strict) when the capture age is known, superseding the arrival/`frame_ts` lag branches; unknown capture time keeps the old arrival-based rule. `_on_native_record` and `_accept_remote` carry the age into the snapshot (the latter adds IPC transit so `updated - age` is never later than the true capture).
- Tests in `test_screen_uxplay.py`: a frame captured 0.1 s before the action, arriving 1.8 s after it (capture age 1.9 s), is rejected by `fresh_frame(since=action)`; a post-action capture arriving late is accepted; no-capture-time keeps the arrival rule; remote snapshots carry the age with transit.

### Regression 2 — paste sheet evidence retention

- `_Transaction` gained `post_paste_frames` (snapshotted in `paste()` after `wait_after_action`) and `sheet_evidence` (set on verify success). `confirm()` first retries the retained evidence, then scans the retained frames + live `recent_frames`; the windowed generation/time checks are unchanged. `paste_confirmation.py` now filters out empty lines from the expected lines (blank lines have no OCR row), so an append starting with a newline is verifiable; the transaction digest still pins the exact text.
- Tests: `test_confirm_sheet_retry.py` gained a window-passed retention test (sheet published during the wait, live frames cleared, confirm still completes) and a retry-reuse test; `test_paste_confirmation.py` gained a newline-leading append test.

### Item 1 — mirror (1a/1b passed on device, 1c pending)

- State at round-3 start: `/api/status` device/screen/calibrated/ready all true, video live, frame_age_s 0.179, generation 5, HID ble conn=1 bond=1. **Worker staleness:** the web worker (pid 71934, started 12:30) was still running pre-round-2 code, which is why `/api/status` showed `replay_lag_s: null` on a live mirror (the receiver socket probe itself returned `capture_age_s: 0.005`). Fixed by `restart-worker` (new worker pid 44998, instance `9621ae1c…`); `replay_lag_s` then reported real values (~0.04–0.18 s). Any code change needs a worker restart before device testing.
- 1a continuous frames: 7 samples over 30 s, all live, frame_age_s 0.150–0.234, replay_lag_s 0.075–0.180 → pass.
- 1b worker-only restart: receiver instance `1a0a4b77…` / pid 24772 unchanged; status after restart device/screen/calibrated/ready all true; video live, frame_age 0.077, replay_lag 0.039; calibration.json SHA-256 identical before/after; pre-restart screen_id rejected with 409 `invalid or consumed screen_id; observe again before acting`; fresh observe works → pass.
- 1c disconnect+reconnect: not yet run — do after items 2–4 so a stranded mirror cannot block them. Round-2 evidence says the iPhone does not auto-reconnect after a receiver restart; expect the user to re-select "ESP32 Agent Mac" in Control Center → Screen Mirroring.

### Item 2 — Home → Spotlight → Safari (failed on device, zero clicks sent)

- Attempts (all from the native Notes test note, keyboard up): 2× `open_app` "Safari" (failed in `go_home`: "离开目标再返回后仍未确认指针落点，未发送点击", 0/75 and 0/~30 pair matches), 3× direct tap on the AssistiveTouch 主屏幕 item (failed at the write guard: "指针定位期间目标或屏幕已变化，未发送点击"), 2× `key cmd+h` (sent, no effect on this iPhone), 1× swipe-up from the bottom edge (failed at the write guard), Esc dismissed the AssistiveTouch menu once of three tries.
- Diagnosis from the pointer diagnostics (offline, production detector): in the `open_app` attempt the pointer disc (44,42,45) is visible in the outward frames at (294, 864) → (294, 843) but absent from all 12 return frames — the stationary iOS pointer fades within ~1 s and the faded disc falls below the 40-min detection floor over the dark menu. In the direct taps the landing was measured once, but the write guard tripped on the Notes caret blink: the caret is saturated yellow (on-pixels ~(186,175,5), off ~white), `field_carets` cannot see it, so `mask_caret_blink` exempts nothing (verified offline: 161 hot px remain before and after masking; guard threshold is 24) and the caret phase at write time differs from the observation frame. The 主屏幕 hover highlight toggling adds further change near the target. One tap on blank note area did succeed (guard passed when the caret phases aligned) and dismissed the menu.
- **Blocked:** needs a detection-code fix (fade-tolerant pairing, yellow-caret recognition, highlight-tolerant menu guard) — out of the round-3 scope; awaiting the user's decision. No Home → Spotlight → Safari end-to-end run exists yet.

### Item 3 — native Notes double-tap word selection (failed on device, zero clicks sent)

- Target: "你好" in "验收：你好 00084" (OCR line box [0.066, 0.219, 0.443, 0.242], center (0.2545, 0.2303)). 2 attempts: plain `double_tap` ("蓝牙指针纠偏后未能确认落点在目标内，未发送点击", 0/32 pair matches) and `double_tap` with the line box as `target_box` (escape probe + extension ran, 0/78 pair matches).
- Diagnosis: the pointer is detectable on blank canvas (extended outward frames show it at (125, 185), (157,174,166)) but undetectable on the return frame when it sits on text glyphs (occluded + faded), so the return pair never matches. The yellow-caret guard is a second blocker for the write step.
- **Blocked:** same detection-code fix decision as item 2. No selection screenshot exists yet.

### Item 4 — native Notes paste (blocked on the iPhone-side binding)

- New text (not reused from the verified set): `"\n秋天到了00731\n第二行 AbD eFg"` — starts with a newline (regression-2 coverage), new Chinese words, leading-zero `00731`, mixed case `AbD eFg`. Baseline note content at prepare time: alphatargetomegadelta / alphatargetomega / 验收：你好 00084 / 核对：AbC xylem.
- `prepare` (transaction `8BPX4-X_9JaVO2Lq9YWckC4UlSfwMHzJhmE1PMItD8s`, focus verified by host_observation with target_box [0.03, 0.10, 0.97, 0.55]) succeeded: phase `awaiting_copy`, trigger key `ctrl+alt+p` auto-sent once. Polled `status` 24 times over 120 s: `copied` stayed false, phase `awaiting_copy`. The iPhone-side binding (Control+Option+P → `ESP32 Agent Clipboard` in Settings → Accessibility → Keyboard & Typing → Full Keyboard Access → Commands) is not taking effect. The user was asked to set/check it; after that, re-run prepare (new transaction), poll `copied=true`, then paste → allow the sheet → confirm → verify the actual Notes content against the `completed` receipt.
- Side effect during testing: a tap accepted the keyboard suggestion "xylem" and changed the note's last line from "核对：AbC xYz" to "核对：AbC xylem". The paste baseline uses the current content, so verification is unaffected; restore "xYz" after the acceptance run.

## Round 4–5 (2026-11-16 evening) — detection fixes, end-to-end regressions, highlight acceptance

### Automated vs real-device results

- **Automated:** full Python suite after `9e2a8c7`: **2234 passed, 1 skipped**; after `9372f80`: **2239 passed, 1 skipped**; after `f8a8651`: **2240 passed, 1 skipped** (2026-11-16, ~423 s).
- **Real-device:** item 3 passed (Notes double-tap selects "你好"); item 2 still blocked (zero clicks sent — see below); item 4 blocked on the iPhone-side shortcut binding; item 1c not yet run (do last); the test note's line 3 still carries a stray "xYz" from an earlier suggestion-bar misfire (restore blocked, see below).

### Round 4 — detection fixes (`9e2a8c7`)

Offline diagnosis on the round-3 real frames pinned each failure to a layer:

- **Fix 1 — yellow caret:** `caret.py` `_amber_tone` required `r - g >= 14`; sampled on-phase pixels of the lemon caret (186,175,5) have `r - g` in {9..36}, so `mask_caret_blink` masked nothing (161 hot px before and after; guard threshold 24). Threshold is now `r - g >= 0` (with `r >= 165`, `g - b >= 48`). Regression test fails under the old predicate.
- **Fix 2 — faded pointer over text:** the return frame matched the reference, so `cursor_overlay_pair` returned no overlays at all. Added `_faded_pointer_disc` (pairing-only, never for masking; silhouette symdiff cap 0.12 → 0.25, no core/antialias fit) and `translucent_pointer_pair(before, after, ...)` which diffs the two motion frames directly. Verified on the dt2 frames: outward (126,188) → return (126,241); a mid-return frame is safely rejected. Fixtures in `tests/fixtures/dt2_text_pointer/`.
- **Fix 3 — faded pointer on the dark menu:** the disc over the dark menu is literally indistinguishable (mean RGB delta 0.0). The hover highlight is the evidence: the 主屏幕 item turns blue (icon + label) while the others stay white (171 blue px in the oa2 frame, 205–213 in the item-2 frames, 0 elsewhere). `assistive_vision.menu_home_highlighted(frame, panel)` counts blue pixels per menu slot; `phone._menu_highlight_landing` uses it as a fallback in `point_verified`.
- **Fix 4 — keyboard suggestion bar:** `input_context.keyboard_suggestion_bar(frame)` finds the keyboard top from grey-row bands; `point_in_suggestion_bar` guards `tap_verified` so a bare tap whose requested point falls in the bar is refused with "点击落在键盘建议栏…". This is the negative case for the earlier misfire that changed the note's last line to "xylem".
- **Fix 4b:** the detector misread a uniform grey background as a keyboard (legacy `direct_tap` regression). It now also requires a brightness jump (> 0.30) at the keyboard top and non-grey content above it.

### Round 5 — end-to-end regressions (`9372f80`)

- **Regression 1 — Home-highlight click flow** (`test_menu_highlight_click_flow.py`, real white→blue frames): `menu_home_highlight` now returns a `MenuHomeHighlight` (highlighted, centroid, blue count, ratio, other_max); `phone._menu_highlight_landing` returns the measured centroid and records evidence separately (`before_blue`, `after_blue`, `after_other_max`, `centre`); `TextTargetGuard` allows only this move's Home highlight change (masks the Home slot, and `_home_highlight_excuses_old_discs` excuses the pre-move disc that disappears when it turns blue). Valid highlight sends exactly one click; static highlight or a changed target sends zero.
- **Regression 2 — bare-tap suggestion-bar guard** (`test_bare_tap_suggestion_bar_flow.py`): `tap_verified.validate_click` checks the measured landing (not the requested point) against the latest frame's suggestion bar before sending; "request outside the bar, landing inside the bar" sends zero clicks.

### Highlight acceptance fix (`f8a8651`)

- Live diagnosis (temporary HL-DIAG logging, since reverted): `_menu_highlight_landing` **did** return the measured centroid (0.5837, 0.8395) with before_blue=0 → after_blue=205, but `point_verified.on_target` rejected it — the centroid sits 0.0085 from the target centre in y, just outside `AIM_TOLERANCE` (0.008), and `paired_ble` is False because the disc is invisible over the blue highlight, so the target-box interior fallback never ran.
- Fix: a highlight centroid inside the target box is itself the landing evidence (the blue highlight only appears while the pointer hovers Home), so `on_target` accepts it without demanding paired BLE verification. New regression test: a centroid inside the box but 0.010 outside `AIM_TOLERANCE` in y still sends exactly one click.

### Item 2 — Home → Spotlight → Safari (still blocked on device, zero clicks sent)

- After `f8a8651` and a worker restart, the menu-open attempt's landing was accepted (the failure moved to the write guard: "指针定位期间目标或屏幕已变化"), and the menu then dismissed without going Home.
- From a clean Notes state, `home` fails at the AssistiveTouch **bubble** tap: the bubble is a small translucent grey circle and its landing cannot be confirmed ("离开目标再返回后仍未确认指针落点"); a direct bare tap on it reports "无法确认指针落点：需要当前观察中的已知目标及其大小，未发送点击". The menu never opens.
- `worker.log` confirms the `go_home` fallbacks actually tried are assistive-touch → homescreen → cmd+h (no swipe-up entry in the log); all three fail. `cmd+h` has no effect on this iPhone (11 Pro, iOS 17.5.1).
- **Blocked:** needs a way past the bubble tap (or a guard that tolerates the already-highlighted menu). No Home → Spotlight → Safari end-to-end run exists yet.

### Item 3 — native Notes double-tap word selection (passed on device)

- After the round-4 worker restart, `double_tap` on "你好" (line box [0.066, 0.219, 0.441, 0.242], centre (0.2535, 0.2303)) returned http=200; the after frame shows "你好" selected (blue highlight) with the selection menu open (粘贴/选择/全选/插入涂鸦). Evidence: `r4-item3-after-dt.jpg`.

### Restore of the test note (blocked)

- Line 4 is fixed (`核对：AbC xYz`). Line 3 still reads `验收：xYz你好 00084` — the stray "xYz" before "你好" must be removed so it reads `验收：你好 00084`. Six approaches failed, all blocked by pointer-landing confirmation on the thin Latin glyphs (bare tap, bare double-tap, tap/double-tap with the line box, `tap_text "你好"` needs an explicit region, `key "up"` is unsupported). The item-3 double-tap on the wide Chinese glyphs at nearly the same coordinates succeeded, so this is specific to small Latin text. Do not keep hammering the same taps; it may need a separate fix or the user deleting the 3 characters manually.

### Item 4 — native Notes paste (blocked on the iPhone-side binding)

- The round-3 transaction expired without being fetched (`needs_resolution: False` — the Shortcut never claimed the text). The user was asked to bind Control+Option+P → **"ESP32 Agent Clipboard"** (Settings → Accessibility → Keyboard & Typing → Full Keyboard Access → Commands). Not yet confirmed.
- A parallel branch `muse/paste-shortcut-regression` (worktree `esp32-iphone-agent-hybrid-paste`, baseline `9e2a8c7`, commit `71fa712`) adds paste-status diagnostics (`claimed`, `pending_polls`, `claim_attempts`, `ack_attempts`, `last_ack_error`, `pending_auth_failures` — counts and timestamps only) so the three failure modes (not launched / not claimed / not acked) can be told apart, plus the newline-leading append sheet-evidence regression. Full suite there: 2243 passed, 1 skipped. Note: the binding must point at the staged template "ESP32 Agent Clipboard" — the old "领取待粘贴文本" shortcut waits for the user to tap a notification before returning ACK, which produces the same copied=false symptom.
