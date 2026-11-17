"""A paired landing permits one relative correction without inventing absolute state."""

import asyncio
import io
import time

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_verified_tap import LaggingMirror

from hidagent.phone import Phone
from hidagent.text_target import PointerLandingError

TARGET = (.58125, .7075289575289575)
LANDED = (.567, .6986111111111111)
BOX = (.5583333333333333, .696911196911197, .6041666666666666, .718146718146718)


def frame(pointer=None, *, changed=False):
    image = Image.new("RGB", (500, 1080), (40, 50, 60))
    draw = ImageDraw.Draw(image)
    draw.text((230, 820), "Home target", fill="white")
    if changed:
        draw.rectangle((210, 800, 350, 850), fill="red")
    if pointer:
        x, y = pointer[0] * 500, pointer[1] * 1080
        draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=(145, 145, 145))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def rig(tmp_path, monkeypatch, *, result="success", interrupt=None):
    clock = ManualClock()
    clock.install(monkeypatch)

    class Mirror(LaggingMirror):
        generation = 1

        def publish(self, image):
            self.frame, self.updated = image, time.time()

        async def send_guarded(self, command, before_write, **kwargs):
            if command == "move 4 5":
                self.relative_write_attempted = True
                if interrupt == "generation":
                    self.generation += 1
                elif interrupt == "link":
                    self.links += 1
                elif interrupt == "abort":
                    self.abort_epoch += 1
                elif interrupt == "stale":
                    self.updated = time.time() - 6
                elif interrupt == "layout":
                    self.publish(frame(LANDED, changed=True))
                elif interrupt == "pose":
                    self.publish(frame((.64, .70)))
            before_write()
            await self.send(command)

        async def send(self, command):
            self.commands.append(command)
            if command == "move 0 -22":
                image = frame((.563, .6597222222222222))
            elif command == "move 0 22":
                image = frame(LANDED)
            elif command == "move 4 5":
                if result == "unknown":
                    raise RuntimeError("relative correction delivery unknown")
                self.corrected = True
                image = frame(TARGET if result == "success" else (.54, .67) if result == "outside" else None)
            elif getattr(self, "corrected", False):
                image = frame()  # Failed new measurement cannot recycle the old pair.
            else:
                image = frame(LANDED)  # The standard horizontal probe is not visible.
            for delay in (.1, .4, .8, 1.2, 1.75, 1.9):
                clock.call_later(delay, self.publish, image)
            await asyncio.sleep(.02)

    mirror = Mirror()
    mirror.frame, mirror.updated = frame(), time.time()
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    phone.span, phone._pace = {"w": 270, "h": 584}, 4
    phone._pos, phone._link, phone._clock_link = (0, 0), 0, 0
    monkeypatch.setattr(phone, "_save", lambda: None)

    async def no_effect(*args):
        return False, True

    monkeypatch.setattr(phone, "_tap_effect", no_effect)
    return phone, mirror


def test_real_probe_failure_then_paired_escape_can_correct_without_absolute_position(tmp_path, monkeypatch):
    phone, mirror = rig(tmp_path, monkeypatch)
    result = asyncio.run(phone.tap_verified(*TARGET, target_box=BOX, target_frame=mirror.frame,
                                           allow_estimated_landing=False))
    assert result["clicked"]
    assert mirror.commands == ["move 157 413", "move 12 0", "move -12 0",
                               "move 0 -22", "move 0 22", "move 4 5", "click"]
    assert phone._pointer_confirmed
    assert phone._pos is None


@pytest.mark.parametrize("result", ["failed", "unknown", "outside"])
def test_relative_correction_still_needs_new_measurement(tmp_path, monkeypatch, result):
    phone, mirror = rig(tmp_path, monkeypatch, result=result)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.tap_verified(*TARGET, target_box=BOX, target_frame=mirror.frame,
                                      allow_estimated_landing=False))
    assert mirror.commands.count("move 4 5") == 1
    assert "click" not in mirror.commands and "press" not in mirror.commands
    assert not phone._pointer_confirmed and phone._pos is None


@pytest.mark.parametrize("interrupt", ["generation", "link", "abort", "stale", "layout", "pose"])
def test_relative_correction_checks_original_evidence_at_write(tmp_path, monkeypatch, interrupt):
    phone, mirror = rig(tmp_path, monkeypatch, interrupt=interrupt)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.tap_verified(*TARGET, target_box=BOX, target_frame=mirror.frame,
                                      allow_estimated_landing=False))
    assert mirror.commands == ["move 157 413", "move 12 0", "move -12 0", "move 0 -22", "move 0 22"]
    assert mirror.relative_write_attempted
    assert not phone._pointer_confirmed and phone._pos is None


@pytest.mark.parametrize("missing", ["pair", "target", "overlay"])
def test_unknown_absolute_position_cannot_correct_without_measured_evidence(tmp_path, monkeypatch, missing):
    phone, mirror = rig(tmp_path, monkeypatch)

    async def landing(*args, **kwargs):
        phone._pos = None
        phone._pointer_pair_verified = missing != "pair"
        mirror.publish(frame(LANDED))
        phone._pointer_overlay = None if missing == "overlay" else (LANDED, mirror.frame)
        return LANDED

    monkeypatch.setattr(phone, "_find_pointer", landing)
    with pytest.raises(PointerLandingError):
        asyncio.run(phone.tap_verified(*TARGET, target_box=None if missing == "target" else BOX,
                                      target_frame=mirror.frame, allow_estimated_landing=False))
    assert mirror.commands == ["move 157 413"]
    assert not phone._pointer_confirmed and phone._pos is None
