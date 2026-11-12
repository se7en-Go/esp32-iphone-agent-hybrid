Real frames from `/api/frame.jpg` (720x1560 JPEG), iPhone 15 Pro Max, iOS 26.3.1, AssistiveTouch on
(its button rests at about (650, 1230) px). The iOS 26 pointer is a mid-grey filled disc, about 31 px wide.

| file | sent before it | pointer centre (px) |
| --- | --- | --- |
| 00_home_1500ms.jpg | `home` | (91, ~0), half hidden at the top: home stops at the rounded corner |
| 01_m150_2000ms.jpg | `move 150 150` | (250.9, 158.8) |
| 02_nudge_r0_400ms.jpg | `move 12 0` | (260.9, 158.8) |
| 04_m200_200ms.jpg | `move 200 200`, 0.2 s later, still on its way | (367.5, 275.3) |
| 04_m200_2000ms.jpg | `move 200 200` | (463.5, 371.3) |
| 05_nudge_r_400ms.jpg | `move 12 0` | (473.5, 371.3) |

Live, 15:07 the same day, verified tap at (0.75, 0.40) = (540, 624) px after calibrating to span 678 x 1466,
origin (0.127, -0.001):

| file | what | pointer centre (px) |
| --- | --- | --- |
| live_tap_landed.jpg | after home + `move 422 587` | (570.1, 663.7): long moves go 30/40 px further |
| live_tap_landed_nudged.jpg | + `move 12 0` | (580.7, 663.6) |
| live_tap_corrected.jpg | after the correction `move -28 -37` | (540.5, 624.0) |
| live_tap_corrected_nudged.jpg | + `move 12 0` | (545.6, 624.0): the same nudge moved only 5.1 px |

`keypad_no_disc.jpg`: `/workspace/hybrid-ptr/dial_final.jpg`, resized to 360x780, JPEG quality 85.
Real Phone keypad showing the completed dial test; no visible pointer disc. Tests draw explicit
synthetic highlights, clock changes and digit changes on this image; the capture alone does not
establish whether the missing disc was faded or morphed into a control highlight.

`airplay_receiver_button.jpg` is a 360x780 downsample of
`/workspace/hybrid-ptr/live/cal-144728/1794466059.430_n+0.0254.jpg` (about 16 KB).
It preserves the receiver icons and the AssistiveTouch button at normalized
`(655/720, 1220/1560)` for button detection without cached coordinates. The
receiver glyphs must not be mistaken for the button's concentric rings.

OCR fixture `dial_final.jpg` is the original 720x1560 JPEG from
`/workspace/hybrid-ptr/dial_final.jpg` (53 KB): Phone keypad displaying `132 6717 9555`
without placing a call. OCR tests also reuse `00_home_1500ms.jpg` for Chinese Settings
labels and the English word `iPhone`.
