"""Safety and signed-coordinate regressions for steering A/E1."""
import asyncio
import time

import pytest
from test_verified_tap import KEYPAD, LaggingMirror, calibrated_phone


def test_signed_offsets_reach_left_and_above_home(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone.origin = {"x": .127, "y": .12}
    assert phone._dx(.08) < 0
    assert phone._dy(.08) < 0
    asyncio.run(phone.point(.08, .08))
    assert any(command.startswith("move -") for command in mirror.commands)
    assert phone._pointer_position() == pytest.approx((.08, .08), abs=.001)


def test_unknown_target_never_blind_clicks(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    async def hidden(*args, **kwargs):
        return None
    phone._find_pointer = hidden
    with pytest.raises(RuntimeError, match="目标|落点"):
        asyncio.run(phone.tap_verified(.5, .44))
    assert "click" not in mirror.commands


@pytest.mark.parametrize("samples,box", [([], (.4, .35, .6, .55)),
                                        ([(.2, .2), (.5, .55), (.8, .8)], (.49, .43, .51, .45))])
def test_unmeasured_or_oversized_landing_error_refuses(tmp_path, samples, box):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    for axis in ("x", "y"):
        phone._seen[axis].extend(samples)
    async def hidden(*args, **kwargs):
        return None
    phone._find_pointer = hidden
    with pytest.raises(RuntimeError, match="落点|校准"):
        asyncio.run(phone.tap_verified(.5, .44, target_box=box, target_frame=KEYPAD))
    assert "click" not in mirror.commands


def test_stale_frame_never_clicks(tmp_path):
    mirror = LaggingMirror()
    mirror.updated = time.time() - 10
    phone = calibrated_phone(tmp_path, mirror)
    async def visible(*args, **kwargs):
        return .5, .44
    phone._find_pointer = visible
    with pytest.raises(RuntimeError, match="新鲜"):
        asyncio.run(phone.tap_verified(.5, .44))
    assert "click" not in mirror.commands


def test_left_origin_target_uses_signed_relative_move_from_known_position(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone._pos = (phone._dx(.5), phone._dy(.3))
    asyncio.run(phone.point(.08, .3))
    assert mirror.commands == [f"move {phone._dx(.08) - phone._dx(.5)} 0"]
    assert phone._pointer_position() == pytest.approx((.08, .3), abs=.001)


def test_recent_pointer_is_observed_without_waking_again(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone._last_motion = time.monotonic()
    asyncio.run(phone._find_pointer((.5, .44), quick=True))
    assert not mirror.commands


def test_idle_pointer_wakes_only_when_search_is_requested(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone._last_motion = 0
    asyncio.run(phone._find_pointer((.5, .44), quick=True))
    assert mirror.commands == ["move 2 0", "move -2 0"]


def test_public_tap_cannot_bypass_blind_landing_guard(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)

    async def hidden(*args, **kwargs):
        return None

    # Isolate the public wrapper's landing guard from the silent-stream search timeout.
    phone._find_pointer = hidden
    with pytest.raises(RuntimeError, match="目标|落点"):
        asyncio.run(phone.tap(.5, .44))
    assert "click" not in mirror.commands


def test_bounded_target_does_not_wake_idle_pointer(tmp_path, monkeypatch):
    from test_verified_tap import observed_tap

    from hidagent import phone as phone_module
    monkeypatch.setattr(phone_module, "TAP_EFFECT_WAIT", 0)
    monkeypatch.setattr(phone_module.feedback, "MIRROR_LAG_SECONDS", 0)
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone._pos = (phone._dx(.5), phone._dy(.44))
    result = asyncio.run(observed_tap(phone, .5, .44))
    assert mirror.commands == ["click"]
    assert result["effect_verified"] is False and result["effect_observed"] is None


@pytest.mark.parametrize("lag", [.2, 1.7])
def test_positioning_motion_pairs_pointer_without_a_wake(tmp_path, monkeypatch, lag):
    from test_verified_tap import disc_frame

    from hidagent import phone as phone_module
    monkeypatch.setattr(phone_module, "TAP_EFFECT_WAIT", 0)

    class MovingMirror(LaggingMirror):
        async def send(self, command):
            await super().send(command)
            if command == "move 25 0":
                image = disc_frame(275)
                self.pending.append((time.monotonic() + lag, image))

        async def fresh(self, since, timeout):
            result = await super().fresh(since, timeout)
            if result:
                self.frame_ts = self.updated * 1000
            return result

    mirror = MovingMirror(paced=True)
    mirror.frame, mirror.frame_ts = disc_frame(250), time.time() * 1000
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone.origin, phone._pos = {"w": 498, "h": 1080}, {"x": 0, "y": 0}, (250, 500)
    phone._clock, phone._clock_link = 0, 0
    if lag < phone_module.freshness.MIRROR_LAG_SECONDS:
        with pytest.raises(RuntimeError, match="落点|目标|新鲜"):
            asyncio.run(phone.tap_verified(275 / 498, 500 / 1080))
        assert "click" not in mirror.commands
    else:
        result = asyncio.run(phone.tap_verified(275 / 498, 500 / 1080))
        assert result["pointer_seen"] is True
        assert mirror.commands == ["move 25 0", "click"]


def test_cold_pointer_localization_validates_third_landing(tmp_path, monkeypatch):
    from test_verified_tap import disc_frame

    from hidagent import phone as phone_module
    monkeypatch.setattr(phone_module, "TAP_EFFECT_WAIT", 0)

    class LocalizationMirror(LaggingMirror):
        def __init__(self):
            super().__init__()
            self.pos = [0, 0]
            self.frame_ts = time.time() * 1000

        async def send(self, command):
            await super().send(command)
            if command == "home":
                self.pos = [0, 0]
            elif command.startswith("move "):
                dx, dy = map(int, command.split()[1:3])
                self.pos = [self.pos[0] + dx, self.pos[1] + dy]
                image = disc_frame(self.pos[0])
                self.pending.append((time.monotonic() + 1.7, image))

        async def fresh(self, since, timeout):
            frame = await super().fresh(since, timeout)
            if frame:
                self.frame_ts = self.updated * 1000
            return frame

    mirror = LocalizationMirror()
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone.origin, phone._pos = {"w": 498, "h": 1080}, {"x": 0, "y": 0}, None
    phone._clock, phone._clock_link = 0, 0
    for axis in ("x", "y"):
        phone._seen[axis].extend([(.1, .1), (.8, .8)])
    # The baseline has no pointer disc; the move/return pair must establish one before clicking.
    mirror.frame = disc_frame(-100)
    result = asyncio.run(phone.tap_verified(275 / 498, 500 / 1080))
    assert result["pointer_seen"] is True
    assert mirror.commands == ["home", "move 275 500", "move 12 0", "move -12 0", "click"]
    assert len(phone._seen["x"]) == len(phone._seen["y"]) == 3


def test_static_source_and_new_circle_do_not_prove_pointer_motion(tmp_path, monkeypatch):
    import io

    from PIL import Image, ImageDraw
    from test_verified_tap import disc_frame

    from hidagent import phone as phone_module
    monkeypatch.setattr(phone_module.feedback, "MIRROR_LAG_SECONDS", .01)
    before = disc_frame(250)
    image = Image.open(io.BytesIO(before)).convert("RGB")
    ImageDraw.Draw(image).ellipse((263, 488, 287, 512), fill=(150, 150, 150))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    mirror = LaggingMirror()
    mirror.frame, mirror.frame_ts = buffer.getvalue(), time.time() * 1000
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone.origin = {"w": 498, "h": 1080}, {"x": 0, "y": 0}
    phone._clock, phone._clock_link = 0, 0
    phone._position_before, phone._position_from = before, (250 / 498, 500 / 1080)
    phone._position_completed = time.time() - .01
    result = asyncio.run(phone._observe_landing((275 / 498, 500 / 1080), 0))
    assert result is None
    assert not mirror.commands and not phone._seen["x"]


def test_visible_pointer_outside_tiny_known_target_is_not_confirmed(tmp_path):
    mirror = LaggingMirror()
    phone = calibrated_phone(tmp_path, mirror)

    async def nearby_but_outside(*args, **kwargs):
        return .507, .5  # inside general .008 tolerance, outside this actual target

    phone._find_pointer = nearby_but_outside
    with pytest.raises(RuntimeError, match="落点"):
        asyncio.run(phone.tap_verified(.5, .5, target_box=(.497, .497, .503, .503), target_frame=mirror.frame))
    assert not phone._pointer_confirmed and "click" not in mirror.commands
