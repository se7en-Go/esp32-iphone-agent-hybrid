"""A verified start correction must not be drawn back into the first stroke segment."""

import asyncio
import math

import pytest
from test_assistive_fallback import app_frame
from test_review3_gestures import Link

from hidagent.device import Device
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.fixture
def rig(tmp_path):
    screen, device, link = Screen(), Device(), Link()
    screen._on_jpeg(app_frame())
    phone = Phone(device, tmp_path / "calibration.json", screen)
    phone.span = {"w": 400, "h": 800}
    return phone, screen, device, link


@pytest.mark.parametrize("shape", ["short_vertical", "closed_circle"])
def test_segments_preserve_the_measured_start_correction(rig, shape):
    phone, _screen, device, link = rig
    points = ([(.669, .443), (.668, .48), (.668, .49)] if shape == "short_vertical"
              else [(.5 + .245 * math.cos(i * math.pi / 8),
                     .46 + .1134 * math.sin(i * math.pi / 8)) for i in range(17)])
    anchor = (211, 317)  # Actual corrected board pose, deliberately unlike the theoretical start.

    async def positioned(*_args, **_kwargs):
        phone._pos, phone._link = anchor, device.links
        phone._pointer_confirmed = True
        return points[0]

    phone.point_verified = positioned

    async def run():
        device.attach(link)
        try:
            origin = phone._dx(points[0][0]), phone._dy(points[0][1])
            expected, previous = ["press"], origin
            for x, y in points[1:]:
                target = phone._dx(x), phone._dy(y)
                if target != previous:
                    expected.append(f"move {target[0] - previous[0]} {target[1] - previous[1]}")
                previous = target
            await phone.drag(points, hold=False, verify_start=True)
            assert link.written == [*expected, "release"]
            assert phone._pos == (anchor[0] + previous[0] - origin[0],
                                  anchor[1] + previous[1] - origin[1])
        finally:
            device.close()

    asyncio.run(run())


def test_missing_measured_board_pose_never_presses(rig):
    phone, _screen, device, link = rig

    async def positioned(*_args, **_kwargs):
        phone._pos, phone._link = None, device.links
        phone._pointer_confirmed = True

    phone.point_verified = positioned

    async def run():
        device.attach(link)
        try:
            with pytest.raises(RuntimeError, match="指针位置"):
                await phone.drag([(.5, .4), (.5, .5)], hold=False, verify_start=True)
            assert "press" not in link.written
        finally:
            device.close()

    asyncio.run(run())


@pytest.mark.parametrize("transition", ["epoch", "generation"])
def test_corrected_stroke_still_stops_between_segments_on_session_change(rig, transition):
    phone, screen, device, link = rig

    async def positioned(*_args, **_kwargs):
        phone._pos, phone._link = (230, 300), device.links
        phone._pointer_confirmed = True

    phone.point_verified = positioned
    original_write = link.write

    def write(data):
        original_write(data)
        if b" move " in data:
            if transition == "epoch":
                device.request_abort()
            else:
                screen._on_ready()
                screen._on_jpeg(app_frame())

    link.write = write

    async def run():
        device.attach(link)
        try:
            with pytest.raises(RuntimeError, match="停止|连接"):
                await phone.drag([(.5, .4), (.5, .5), (.5, .6)], hold=False, verify_start=True)
            assert link.written == ["press", "move 0 80", "release"]
        finally:
            device.close()

    asyncio.run(run())
