import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from hidagent import phone as phone_module
from hidagent.phone import Phone
from hidagent.text_target import TextTargetGuard


def frame(pointer=None, *, changed=False):
    image = Image.new("RGB", (720, 1560), "white")
    draw = ImageDraw.Draw(image)
    draw.text((300, 600), "Settings", fill="black", font_size=32)
    if changed:
        draw.rectangle((250, 650, 500, 700), fill="black")
    if pointer:
        x, y = pointer
        draw.ellipse((x - 15, y - 15, x + 15, y + 15), fill=(150, 150, 150))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_verified_tap_runs_guard_after_real_pointer_positioning(tmp_path, monkeypatch):
    events = []
    screen = SimpleNamespace(live=True, frame=frame())

    async def send(command):
        events.append(command)

    phone = Phone(SimpleNamespace(send=send, links=0), tmp_path / "calibration.json", screen)
    phone.span = {"w": 720, "h": 1560}

    async def locate(*args, **kwargs):
        events.append("located")
        screen.frame = frame(changed=True)
        return .5, .4

    phone._find_pointer = locate

    def reject():
        events.append("guard")
        assert screen.frame == frame(changed=True)
        raise RuntimeError("screen changed; no click")

    monkeypatch.setattr(phone_module, "TAP_GAP", 0)
    with pytest.raises(RuntimeError, match="no click"):
        asyncio.run(phone.tap_verified(.5, .4, before_click=reject))
    assert events[-2:] == ["located", "guard"]
    assert "click" not in events


@pytest.mark.parametrize("verified", [False, True])
def test_tap_returns_click_completion_time_after_positioning(tmp_path, monkeypatch, verified):
    clock = [1.0]
    events = []

    async def send(command):
        events.append(command)
        clock[0] = 8.0

    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json")

    async def point(*args):
        clock[0] = 5.0

    phone.point = phone.point_verified = point
    monkeypatch.setattr(phone_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(phone_module, "TAP_GAP", 0)
    if verified:  # tap_verified returns A's tap report; the click ACK time rides in it
        phone._tap_effect = lambda *args: asyncio.sleep(0, (False, False))
        assert asyncio.run(phone.tap_verified(.5, .4))["completed_at"] == 8.0
    else:
        assert asyncio.run(phone.tap(.5, .4)) == 8.0
    assert events == ["click"]


def test_target_guard_allows_only_the_pointer_to_move():
    before = frame((100, 1000))
    screen = SimpleNamespace(live=True, frame=frame((360, 624)))
    target = SimpleNamespace(center=(.5, .4))
    TextTargetGuard(screen, before, target)()


def test_target_guard_rejects_a_local_change_even_with_pointer_movement():
    before = frame((100, 1000))
    screen = SimpleNamespace(live=True, frame=frame((360, 624), changed=True))
    target = SimpleNamespace(center=(.5, .4))
    with pytest.raises(RuntimeError, match="未发送点击"):
        TextTargetGuard(screen, before, target)()


@pytest.mark.parametrize("live,content", [(False, frame()), (True, None), (True, b"invalid")])
def test_target_guard_rejects_lost_or_invalid_frames(live, content):
    screen = SimpleNamespace(live=live, frame=content)
    with pytest.raises(RuntimeError, match="未发送点击"):
        TextTargetGuard(screen, frame(), SimpleNamespace(center=(.5, .4)))()


def test_target_guard_ignores_cursor_correction_on_real_settings_frames():
    fixtures = Path(__file__).parent / "fixtures" / "ios26"
    before = (fixtures / "live_tap_landed.jpg").read_bytes()
    after = (fixtures / "live_tap_corrected.jpg").read_bytes()
    screen = SimpleNamespace(live=True, frame=after)
    TextTargetGuard(screen, before, SimpleNamespace(center=(.75, .4)))()


@pytest.mark.parametrize("changed", [False, True])
def test_guard_in_real_verified_tap_checks_the_post_positioning_frame(tmp_path, monkeypatch, changed):
    before = frame((100, 1000))
    screen = SimpleNamespace(live=True, frame=before)
    sent = []

    async def send(command):
        sent.append(command)

    phone = Phone(SimpleNamespace(send=send, links=0), tmp_path / "calibration.json", screen)
    phone.span = {"w": 720, "h": 1560}

    async def locate(*args, **kwargs):
        screen.frame = frame((360, 624), changed=changed)
        return .5, .4

    phone._find_pointer = locate
    phone._tap_effect = lambda *args: asyncio.sleep(0, (False, False))
    monkeypatch.setattr(phone_module, "TAP_GAP", 0)
    guard = TextTargetGuard(screen, before, SimpleNamespace(center=(.5, .4)))
    if changed:
        with pytest.raises(RuntimeError, match="未发送点击"):
            asyncio.run(phone.tap_verified(.5, .4, before_click=guard))
        assert "click" not in sent
    else:
        asyncio.run(phone.tap_verified(.5, .4, before_click=guard))
        assert sent[-1] == "click"


def test_guard_does_not_hide_a_disappeared_grey_control_without_a_matching_pointer():
    screen = SimpleNamespace(live=True, frame=frame())
    guard = TextTargetGuard(screen, frame((100, 1000)), SimpleNamespace(center=(.5, .4)))
    with pytest.raises(RuntimeError, match="未发送点击"):
        guard()


def test_guard_does_not_ignore_status_bar_when_it_contains_the_target():
    before = frame()
    after = Image.open(io.BytesIO(before))
    ImageDraw.Draw(after).rectangle((300, 20, 350, 40), fill="black")
    output = io.BytesIO()
    after.save(output, "PNG")
    screen = SimpleNamespace(live=True, frame=output.getvalue())
    target = SimpleNamespace(center=(.45, .02), box=(.4, .01, .5, .03))
    with pytest.raises(RuntimeError, match="未发送点击"):
        TextTargetGuard(screen, before, target)()
