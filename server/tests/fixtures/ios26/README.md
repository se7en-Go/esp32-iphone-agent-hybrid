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
