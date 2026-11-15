import asyncio
import io
import time
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
    screen = SimpleNamespace(updated=time.time(), live=True, frame=frame())

    async def send(command):
        events.append(command)

    phone = Phone(SimpleNamespace(send=send, links=0), tmp_path / "calibration.json", screen)
    phone.span = {"w": 720, "h": 1560}

    async def locate(*args, **kwargs):
        events.append("located")
        return .5, .4

    phone._find_pointer = locate

    def reject():
        events.append("guard")
        assert events[-2] == "located"
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

    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json",
                  SimpleNamespace(live=True, frame=frame(), updated=1.0))

    async def point(*args):
        clock[0] = 5.0
        phone._pointer_confirmed = True
        phone.screen.updated = 5.0

    phone.point = phone.point_verified = point
    monkeypatch.setattr(phone_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(phone_module, "TAP_GAP", 0)
    phone._tap_effect = lambda *args: asyncio.sleep(0, (False, False))
    if verified:  # tap_verified returns A's tap report; the click ACK time rides in it
        assert asyncio.run(phone.tap_verified(.5, .4))["completed_at"] == 8.0
    else:
        assert asyncio.run(phone.tap(.5, .4)) == 8.0
    assert events == ["click"]


def test_target_guard_allows_only_the_pointer_to_move():
    before = frame((100, 1000))
    screen = SimpleNamespace(updated=time.time(), live=True, frame=frame((360, 624)))
    target = SimpleNamespace(center=(.5, .4))
    TextTargetGuard(screen, before, target)()


def test_target_guard_rejects_a_local_change_even_with_pointer_movement():
    before = frame((100, 1000))
    screen = SimpleNamespace(updated=time.time(), live=True, frame=frame((360, 624), changed=True))
    target = SimpleNamespace(center=(.5, .4))
    with pytest.raises(RuntimeError, match="未发送点击"):
        TextTargetGuard(screen, before, target)()


def test_target_guard_rechecks_the_crop_of_an_observed_cursor_overlay():
    before = frame()
    landing = frame((360, 624))
    screen = SimpleNamespace(updated=time.time(), live=True, frame=landing)
    guard = TextTargetGuard(screen, before, SimpleNamespace(center=(.5, .4)))
    cursor = ((.5, .4), landing)
    guard(cursor=cursor)

    noisy = Image.open(io.BytesIO(landing))
    for x in range(350, 371, 3):
        noisy.putpixel((x, 624), (180, 180, 180))
    out = io.BytesIO()
    noisy.save(out, "PNG")
    screen.frame = out.getvalue()
    guard(cursor=cursor)

    edited = Image.open(io.BytesIO(landing))
    ImageDraw.Draw(edited).rectangle((354, 620, 366, 629), fill="black")
    out = io.BytesIO()
    edited.save(out, "PNG")
    screen.frame = out.getvalue()
    with pytest.raises(RuntimeError, match="未发送点击"):
        guard(cursor=cursor)


@pytest.mark.parametrize("live,content", [(False, frame()), (True, None), (True, b"invalid")])
def test_target_guard_rejects_lost_or_invalid_frames(live, content):
    screen = SimpleNamespace(updated=time.time(), live=live, frame=content)
    with pytest.raises(RuntimeError, match="未发送点击"):
        TextTargetGuard(screen, frame(), SimpleNamespace(center=(.5, .4)))()


def test_target_guard_ignores_cursor_correction_on_real_settings_frames():
    fixtures = Path(__file__).parent / "fixtures" / "ios26"
    before = (fixtures / "live_tap_landed.jpg").read_bytes()
    after = (fixtures / "live_tap_corrected.jpg").read_bytes()
    screen = SimpleNamespace(updated=time.time(), live=True, frame=after)
    TextTargetGuard(screen, before, SimpleNamespace(center=(.75, .4)))()


@pytest.mark.parametrize("changed", [False, True])
def test_guard_in_real_verified_tap_checks_the_post_positioning_frame(tmp_path, monkeypatch, changed):
    before = frame((100, 1000))
    screen = SimpleNamespace(updated=time.time(), live=True, frame=before)
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
    screen = SimpleNamespace(updated=time.time(), live=True, frame=frame())
    guard = TextTargetGuard(screen, frame((100, 1000)), SimpleNamespace(center=(.5, .4)))
    with pytest.raises(RuntimeError, match="未发送点击"):
        guard()


def test_guard_does_not_pair_a_disappeared_disc_with_an_unrelated_highlight():
    from test_verified_tap import keypad_edit

    before = keypad_edit(pointer=True, pointer_at=(.7, .65))
    screen = SimpleNamespace(updated=time.time(), live=True, frame=keypad_edit(highlight=True))
    guard = TextTargetGuard(screen, before, SimpleNamespace(center=(.5, .8)))
    with pytest.raises(RuntimeError, match="未发送点击"):
        guard()


def test_guard_does_not_ignore_status_bar_when_it_contains_the_target():
    before = frame()
    after = Image.open(io.BytesIO(before))
    ImageDraw.Draw(after).rectangle((300, 20, 350, 40), fill="black")
    output = io.BytesIO()
    after.save(output, "PNG")
    screen = SimpleNamespace(updated=time.time(), live=True, frame=output.getvalue())
    target = SimpleNamespace(center=(.45, .02), box=(.4, .01, .5, .03))
    with pytest.raises(RuntimeError, match="未发送点击"):
        TextTargetGuard(screen, before, target)()


def test_original_target_guard_rejects_identical_reconnect_while_tap_waits_for_interaction(tmp_path):
    from test_verified_tap import LaggingMirror, calibrated_phone, observed_tap

    async def run():
        mirror = LaggingMirror()
        mirror.generation = 1
        phone = calibrated_phone(tmp_path, mirror)
        guard = TextTargetGuard(mirror, mirror.frame, SimpleNamespace(center=(.5, .44)))
        phone._tap_effect = lambda *args: asyncio.sleep(0, (False, False))
        await phone._interaction_lock.acquire()
        task = asyncio.create_task(observed_tap(phone, .5, .44, before_click=guard))
        await asyncio.sleep(0)
        assert not task.done() and not mirror.commands
        # Reconnect to an identical screenshot while Phone waits. Its own new
        # observation sees generation 2; the caller's target still belongs to 1.
        mirror.generation = 2
        mirror.updated = time.time()
        phone._interaction_lock.release()
        with pytest.raises(RuntimeError, match="连接|变化"):
            await task
        assert "click" not in mirror.commands

    asyncio.run(run())


@pytest.mark.parametrize("transition", ["arriving", "departing", "morph"])
@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("edit", [None, "label", "context"])
def test_real_tap_authorizes_highlight_only_and_preserves_content(tmp_path, monkeypatch, transition, scale, edit):
    from test_verified_tap import KEYPAD, LaggingMirror, calibrated_phone, keypad_edit

    def resized(frame):
        image = Image.open(io.BytesIO(frame)).convert("RGB")
        if scale != 1:
            image = image.resize((image.width * scale, image.height * scale))
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    frames = (keypad_edit(pointer=True, pointer_at=(.7, .65)) if transition == "morph" else KEYPAD,
              keypad_edit(highlight=True))
    before, after = map(resized, frames[::-1] if transition == "departing" else frames)
    if edit:
        image = Image.open(io.BytesIO(after))
        bounds = (170, 332, 190, 354) if edit == "label" else (302, 88, 312, 108)
        ImageDraw.Draw(image).rectangle(tuple(value * scale for value in bounds), fill="black")
        output = io.BytesIO()
        image.save(output, "PNG")
        after = output.getvalue()
    mirror = LaggingMirror()
    mirror.frame = before
    phone = calibrated_phone(tmp_path, mirror)
    for axis in ("x", "y"):
        phone._seen[axis].extend([(.1, .1), (.5, .5), (.9, .9)])

    async def hidden_after_positioning(*args, **kwargs):
        mirror.frame, mirror.updated = after, time.time()
        return None

    phone._find_pointer = hidden_after_positioning
    phone._tap_effect = lambda *args: asyncio.sleep(0, (False, False))
    monkeypatch.setattr(phone_module, "TAP_GAP", 0)
    tap = phone.tap_verified(.5, .44, target_box=(.4, .39, .6, .49), target_frame=before)
    if edit:
        with pytest.raises(RuntimeError, match="未发送点击"):
            asyncio.run(tap)
        assert "click" not in mirror.commands
    else:
        result = asyncio.run(tap)
        assert result["clicked"] and not result["pointer_seen"]
        assert mirror.commands[-1] == "click" and mirror.commands.count("click") == 1
    assert any(command.startswith("move ") for command in mirror.commands)
