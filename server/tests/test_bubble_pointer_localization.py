"""Regressions for AssistiveTouch bubble pointer localization and HAFE/2 timing."""

import asyncio
import io
import time
from pathlib import Path

from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_pointer_session import setup_phone

from hidagent import assistive_vision, feedback, screen_compare
from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation
from hidagent.screen_uxplay import UxPlayScreen

DT2_FIXTURES = Path(__file__).parent / "fixtures" / "dt2_text_pointer"
DT2_NEAR = (0.2545, 0.2303)
DT2_REACH = (0.153, 0.235)
DT2_SHIFT = 0.06336


def _bubble_frame(*, pointer=None, edit_inside_bubble=False):
    image = Image.new("RGB", (500, 1080), (212, 212, 222))
    draw = ImageDraw.Draw(image)
    cx, cy = round(0.758 * 500), round(0.950 * 1080)
    # Layered translucent AssistiveTouch bubble matching _translucent_buttons/find_button_target.
    draw.ellipse((cx - 34, cy - 34, cx + 34, cy + 34), fill=(160, 161, 167))
    draw.ellipse((cx - 26, cy - 26, cx + 26, cy + 26), fill=(180, 180, 186))
    draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=(199, 198, 203))
    if edit_inside_bubble:
        # Draw inside context.regions[0] (330..428, 976..1075), outside POINTER_RADIUS (22.5 px) of return_pt.
        draw.rectangle((cx + 26, cy - 28, cx + 40, cy - 14), fill=(220, 40, 40))
    if pointer is not None:
        px, py = round(pointer[0] * 500), round(pointer[1] * 1080)
        draw.ellipse((px - 12, py - 12, px + 12, py + 12), fill=(135, 134, 139))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_translucent_pointer_pair_cropped_matches_dt2_fixture():
    outward = (DT2_FIXTURES / "outward.jpg").read_bytes()
    returned = (DT2_FIXTURES / "return.jpg").read_bytes()
    result = screen_compare.translucent_pointer_pair(
        outward, returned, DT2_NEAR, DT2_SHIFT, DT2_REACH, axis=1)
    assert result is not None
    (ax, ay), (bx, by) = result[0]
    assert abs(ax * 500 - 126) <= 3 and abs(ay * 1080 - 188) <= 3
    assert abs(bx * 500 - 126) <= 3 and abs(by * 1080 - 241) <= 3


def test_translucent_pointer_pair_preserves_disc_near_bounds_edge():
    # A disc near the edge of `bounds` needs its diameter + 4 px ring intact
    # inside the padded crop.
    base = Image.new("RGB", (500, 1080), (240, 240, 240))
    draw = ImageDraw.Draw(base)
    for y in range(200, 700, 14):
        draw.rectangle((80, y, 360, y + 5), fill=(30, 30, 30))

    def stamp(px, py):
        img = base.copy()
        d = ImageDraw.Draw(img)
        d.ellipse((px - 12, py - 12, px + 12, py + 12), fill=(130, 130, 130))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    near = (0.50, 0.50)
    reach = (0.12, 0.08)
    # Place outward disc right inside bounds[0] (x=0.38 -> 190px; put at 194px)
    # and return disc shifted right by +0.06 (30px -> 224px).
    outward = stamp(194, 540)
    returned = stamp(224, 540)
    result = screen_compare.translucent_pointer_pair(
        outward, returned, near, 0.06, reach, axis=0)
    assert result is not None
    (ax, _), (bx, _) = result[0]
    assert abs(ax * 500 - 194) <= 2 and abs(bx * 500 - 224) <= 2


def test_verify_cursor_reference_accepts_bubble_edge_straddling_pointer():
    base = _bubble_frame()
    obs = FrameSnapshot(base, time.time(), None, 1, True)
    context = from_navigation("home", obs, button_hint=(0.758, 0.950))
    assert context is not None and context.kind == "bubble"
    assert assistive_vision.find_button_target(base, near=(0.758, 0.950)) is not None

    # Pre-move pointer parked just left of the bubble, straddling context.regions[0][0].
    straddle = (context.regions[0][0], 0.941)
    reference = _bubble_frame(pointer=straddle)
    outward_pt = (0.758, 0.892)
    return_pt = context.target.center
    outward = _bubble_frame(pointer=outward_pt)
    returned = _bubble_frame(pointer=return_pt)

    ok, detected = screen_compare.verify_cursor_reference(
        reference, outward, returned, (outward_pt, return_pt), None,
        context_regions=context.regions)
    assert ok is True
    assert detected is not None
    assert abs(detected[0] - straddle[0]) <= 0.02

    tampered = _bubble_frame(pointer=return_pt, edit_inside_bubble=True)
    ok_bad, _ = screen_compare.verify_cursor_reference(
        reference, outward, tampered, (outward_pt, return_pt), None,
        context_regions=context.regions)
    assert ok_bad is False


def _uxplay_publisher(screen, clock=None):
    def publish(jpeg, *, capture_age_s):
        if clock is not None:
            clock.advance(0.03)
        screen._accept_remote({
            "instance": "a" * 32, "generation": 0, "running": True, "live": True,
            "updated": time.time(), "arrived": time.monotonic(),
            "capture_age_s": capture_age_s,
        }, jpeg)

    return publish


def test_localize_pointer_skips_hafe2_stale_early_frame_and_uses_post_return(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    phone, device, _ = setup_phone(tmp_path)
    screen = UxPlayScreen(socket_path=tmp_path / "airplay.sock")
    publish = _uxplay_publisher(screen, clock)
    base = _bubble_frame(pointer=(0.659, 0.941))
    outward = _bubble_frame(pointer=(0.758, 0.892))
    returned = _bubble_frame(pointer=(0.758, 0.950))
    publish(base, capture_age_s=0.01)
    phone.screen = screen
    screen._freshness_phone = phone
    obs = FrameSnapshot(base, time.time(), None, screen.generation, True)
    context = from_navigation("home", obs, button_hint=(0.758, 0.950))
    phone._navigation_context = context
    monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", 0.35)

    pair_calls = []
    return_leg = False
    real_overlay = screen_compare.cursor_overlay_pair

    def spy_overlay(ref, bef, aft, *args, **kwargs):
        if return_leg:
            pair_calls.append(aft)
        return real_overlay(ref, bef, aft, *args, **kwargs)

    monkeypatch.setattr(screen_compare, "cursor_overlay_pair", spy_overlay)

    async def send(command):
        nonlocal return_leg
        device.commands.append(command)
        dy = int(command.split()[2])
        if dy < 0:
            publish(outward, capture_age_s=0.05)
        else:
            return_leg = True
            # First arrival on return leg is a HAFE/2 frame captured BEFORE the return move.
            publish(outward, capture_age_s=0.45)

            async def publish_post_return():
                await asyncio.sleep(0.06)
                publish(returned, capture_age_s=0.02)

            asyncio.create_task(publish_post_return())

    device.send = send

    result = asyncio.run(phone._localize_pointer(
        context.target.center, 0, reference=(base, None), probe=(1, -34)))

    assert result is not None
    assert abs(result[0] - context.target.center[0]) <= 0.02
    assert abs(result[1] - context.target.center[1]) <= 0.02
    # The stale HAFE/2 frame (outward on return leg) must never trigger overlay pairing.
    assert pair_calls == [returned]


def test_localize_pointer_recovers_frame_arriving_right_at_capture_deadline(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    phone, device, _ = setup_phone(tmp_path)
    screen = UxPlayScreen(socket_path=tmp_path / "airplay.sock")
    publish = _uxplay_publisher(screen, clock)
    base = _bubble_frame(pointer=(0.659, 0.941))
    outward = _bubble_frame(pointer=(0.758, 0.892))
    returned = _bubble_frame(pointer=(0.758, 0.950))
    publish(base, capture_age_s=0.01)
    phone.screen = screen
    screen._freshness_phone = phone
    obs = FrameSnapshot(base, time.time(), None, screen.generation, True)
    context = from_navigation("home", obs, button_hint=(0.758, 0.950))
    phone._navigation_context = context
    monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", 0.05)

    real_fresh = screen.fresh
    return_waits = 0

    async def fresh_with_late_publish(since, timeout):
        nonlocal return_waits
        if not device.commands or int(device.commands[-1].split()[2]) < 0:
            return await real_fresh(since, timeout)
        return_waits += 1
        result = await real_fresh(since, timeout)
        if return_waits >= 3 and screen.frame != returned:
            # Publish the real return frame right as the deadline wait finishes.
            publish(returned, capture_age_s=0.01)
            return True
        return result

    screen.fresh = fresh_with_late_publish

    async def send(command):
        device.commands.append(command)
        dy = int(command.split()[2])
        if dy < 0:
            publish(outward, capture_age_s=0.01)

    device.send = send

    result = asyncio.run(phone._localize_pointer(
        context.target.center, 0, reference=(base, None), probe=(1, -34)))

    assert result is not None
    assert abs(result[0] - context.target.center[0]) <= 0.02
    assert abs(result[1] - context.target.center[1]) <= 0.02
