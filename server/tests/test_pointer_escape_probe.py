"""One bounded, reversible escape from an observed target never authorizes blind contact."""

import asyncio
import io
import time

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_pointer_session import setup_phone
from test_verified_tap import LaggingMirror, calibrated_phone


@pytest.mark.parametrize("found", [True, False])
def test_missing_standard_pair_tries_only_one_target_sized_escape(tmp_path, monkeypatch, found):
    phone, device, mirror = setup_phone(tmp_path)
    calls = []

    async def missing(*args, **kwargs):
        return None

    async def localize(near, epoch, *, reference=None, probe=None, before_probe=None):
        before_probe()
        calls.append(probe)
        assert probe is not None
        axis, distance = probe
        assert axis in (0, 1) and distance
        actual = near[axis] + distance / phone.span["wh"[axis]]
        assert 0 < actual < 1
        phone._pointer_pair_verified = found
        return near if found else None

    monkeypatch.setattr(phone, "_find_pointer", missing)
    monkeypatch.setattr(phone, "_localize_pointer", localize)
    asyncio.run(phone.point_verified(.579, .702, target_box=(.535, .68, .621, .724)))
    assert len(calls) == 1
    assert calls[0][0] == 1  # alternate axis clears the observed icon on its shorter side
    assert abs(calls[0][1]) > 12
    assert phone._pointer_confirmed is found
    assert not any(command in {"press", "click"} for command in device.commands)


@pytest.mark.parametrize("change", ["generation", "link", "abort"])
def test_no_fallback_probe_after_original_session_changes(tmp_path, monkeypatch, change):
    phone, device, mirror = setup_phone(tmp_path)
    calls = []

    async def missing(*args, **kwargs):
        if change == "generation":
            mirror.generation += 1
        elif change == "link":
            device.links += 1
        else:
            device.abort_epoch += 1
        return None

    async def localize(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(phone, "_find_pointer", missing)
    monkeypatch.setattr(phone, "_localize_pointer", localize)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.point_verified(.5, .5, target_box=(.46, .48, .54, .52)))
    assert not calls


@pytest.mark.parametrize("change", ["generation", "link", "abort"])
def test_escape_selection_cannot_reanchor_onto_a_new_session(tmp_path, monkeypatch, change):
    from hidagent import feedback

    phone, device, mirror = setup_phone(tmp_path)
    choose = phone._target_escape_probe

    async def missing(*args, **kwargs):
        return None

    def change_during_selection(*args):
        probe = choose(*args)
        if change == "generation":
            mirror.generation += 1
        elif change == "link":
            device.links += 1
        else:
            device.abort_epoch += 1
        return probe

    mirror.fresh = missing
    monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", .001)
    monkeypatch.setattr(phone, "_find_pointer", missing)
    monkeypatch.setattr(phone, "_target_escape_probe", change_during_selection)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.point_verified(.579, .702, target_box=(.535, .68, .621, .724)))
    assert device.commands == ["move 208 548"]  # Never issue an escape on the replacement session.


@pytest.mark.parametrize("box", [None, (0, 0, 1, 1)])
def test_no_escape_without_small_observed_target(tmp_path, monkeypatch, box):
    phone, _, _ = setup_phone(tmp_path)
    calls = []

    async def missing(*args, **kwargs):
        return None

    async def localize(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(phone, "_find_pointer", missing)
    monkeypatch.setattr(phone, "_localize_pointer", localize)
    asyncio.run(phone.point_verified(.5, .5, target_box=box))
    assert not calls
    assert not phone._pointer_confirmed


def test_failed_escape_never_falls_back_to_estimated_click(tmp_path, monkeypatch):
    from hidagent import feedback
    from hidagent.text_target import PointerLandingError

    phone, device, mirror = setup_phone(tmp_path)
    for axis in ("x", "y"):
        phone._seen[axis].extend([(.4, .4), (.5, .5), (.6, .6)])

    async def missing(*args, **kwargs):
        return None

    mirror.fresh = missing
    monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", .001)
    monkeypatch.setattr(phone, "_find_pointer", missing)
    with pytest.raises(PointerLandingError, match="未发送点击"):
        asyncio.run(phone.tap_verified(.5, .5, target_box=(.45, .45, .55, .55), target_frame=mirror.frame))
    assert phone._pointer_escape_attempted
    assert not phone._pointer_confirmed
    assert "click" not in device.commands and "press" not in device.commands


@pytest.mark.parametrize("corrected", [True, False])
def test_correction_uses_one_unspent_escape_with_measured_reference(tmp_path, monkeypatch, corrected):
    phone, device, mirror = setup_phone(tmp_path)
    probes = []

    async def initial(*args, **kwargs):
        phone._pointer_pair_verified = True
        return .54, .5

    async def no_disc(*args):
        return None

    async def localize(near, epoch, *, reference=None, probe=None, before_probe=None):
        if before_probe is not None:
            before_probe()
        probes.append((probe, reference))
        if probe is not None and corrected:
            phone._pointer_pair_verified = True
            return near
        return None

    monkeypatch.setattr(phone, "_find_pointer", initial)
    monkeypatch.setattr(phone, "_observe_landing", no_disc)
    monkeypatch.setattr(phone, "_localize_pointer", localize)
    asyncio.run(phone.point_verified(.5, .5, target_box=(.48, .48, .52, .52)))
    assert len(probes) == 2
    assert probes[0][0] is None and probes[1][0] is not None
    assert probes[1][1] == (mirror.frame, (.54, .5))
    assert phone._pointer_confirmed is corrected
    assert phone._pointer_escape_attempted
    assert "click" not in device.commands


def test_correction_cannot_spend_a_second_escape(tmp_path, monkeypatch):
    phone, device, _ = setup_phone(tmp_path)
    probes = []

    async def missing(*args, **kwargs):
        return None

    async def localize(near, epoch, *, reference=None, probe=None, before_probe=None):
        before_probe()
        probes.append(probe)
        if probe is not None:
            phone._pointer_pair_verified = True
            return .54, .5
        return None

    monkeypatch.setattr(phone, "_find_pointer", missing)
    monkeypatch.setattr(phone, "_observe_landing", missing)
    monkeypatch.setattr(phone, "_localize_pointer", localize)
    asyncio.run(phone.point_verified(.5, .5, target_box=(.48, .48, .52, .52)))
    assert len(probes) == 2 and probes[0] is not None and probes[1] is None
    assert not phone._pointer_confirmed
    assert "click" not in device.commands


@pytest.mark.parametrize("transpose", [False, True])
def test_measured_landing_can_use_center_tolerance_and_box_margin_on_different_axes(tmp_path, monkeypatch, transpose):
    phone, device, _ = setup_phone(tmp_path)
    target = (.58125, .70173745)
    landed = (.593, .69768519)
    box = (.55833333, .69111969, .60416667, .71235521)
    if transpose:
        target, landed = target[::-1], landed[::-1]
        box = box[1], box[0], box[3], box[2]

    async def initial(*args, **kwargs):
        phone._pointer_pair_verified = True
        return landed

    async def unnecessary_correction(*args):
        pytest.fail("both axes already have measured safe landing evidence")

    monkeypatch.setattr(phone, "_find_pointer", initial)
    monkeypatch.setattr(phone, "_observe_landing", unnecessary_correction)
    assert asyncio.run(phone.point_verified(*target, target_box=box)) == landed
    assert phone._pointer_confirmed
    assert len(device.commands) == 1


@pytest.mark.parametrize("interrupt", [None, "generation", "link", "abort"])
def test_escape_pairs_new_frames_and_does_not_return_across_a_changed_session(tmp_path, monkeypatch, interrupt):
    clock = ManualClock()
    clock.install(monkeypatch)

    def frame(y=None):
        image = Image.new("RGB", (500, 1080), (40, 80, 120))
        if y is not None:
            ImageDraw.Draw(image).ellipse((278, y - 12, 302, y + 12), fill=(150, 150, 150))
        data = io.BytesIO()
        image.save(data, "JPEG", quality=90)
        return data.getvalue()

    class Mirror(LaggingMirror):
        generation = 1

        async def send(self, command):
            self.commands.append(command)
            delta = int(command.split()[2])
            clock.call_later(.15, self.publish, frame(758 + delta if delta < 0 else 758))
            await asyncio.sleep(.2)

        def publish(self, image):
            self.frame, self.updated = image, time.time()
            if interrupt == "generation":
                self.generation += 1
            elif interrupt == "link":
                self.links += 1
            elif interrupt == "abort":
                self.abort_epoch += 1

    mirror = Mirror()
    mirror.frame = frame(758)
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 500, "h": 1080}, 4
    operation = phone._localize_pointer((.58, 758 / 1080), 0, reference=(frame(), None), probe=(1, -55))
    if interrupt:
        with pytest.raises(RuntimeError):
            asyncio.run(operation)
        assert mirror.commands == ["move 0 -55"]
    else:
        result = asyncio.run(operation)
        assert result == pytest.approx((.58, 758 / 1080), abs=.003)
        assert phone._pointer_pair_verified
        assert mirror.commands == ["move 0 -55", "move 0 55"]


@pytest.mark.parametrize("interrupt", [None, "generation", "link", "abort", "write_failure", "no_landing"])
def test_escape_extends_once_when_actual_outward_circle_still_touches_the_icon(tmp_path, monkeypatch, interrupt):
    clock = ManualClock()
    clock.install(monkeypatch)

    def frame(y=None):
        image = Image.new("RGB", (500, 1080), (39, 37, 40))
        draw = ImageDraw.Draw(image)
        draw.rectangle((270, 735, 325, 780), fill=(106, 201, 226))
        if y is not None:
            color = (49, 47, 50) if y < 735 else (116, 145, 154)
            draw.ellipse((284, y - 12, 308, y + 12), fill=color)
        data = io.BytesIO()
        image.save(data, "JPEG", quality=90)
        return data.getvalue()

    class Mirror(LaggingMirror):
        generation = 1
        total = 0

        async def send(self, command):
            self.commands.append(command)
            if interrupt == "write_failure" and len(self.commands) == 2:
                raise RuntimeError("unknown move outcome")
            self.total += int(command.split()[2])
            # Small BLE moves travel less than the original calibration predicts.
            visible_y = None if interrupt == "no_landing" and self.total == 0 else 758 + round(.75 * self.total)
            clock.call_later(.15, self.publish, frame(visible_y))
            await asyncio.sleep(.2)

        def publish(self, image):
            self.frame, self.updated = image, time.time()
            if len(self.commands) == 2:
                if interrupt == "generation":
                    self.generation += 1
                elif interrupt == "link":
                    self.links += 1
                elif interrupt == "abort":
                    self.abort_epoch += 1

    mirror = Mirror()
    mirror.frame = frame(758)
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 500, "h": 1080}, 4
    operation = phone._localize_pointer((296 / 500, 758 / 1080), 0,
                                        reference=(frame(), None), probe=(1, -40))
    if interrupt not in {None, "no_landing"}:
        with pytest.raises(RuntimeError):
            asyncio.run(operation)
        assert len(mirror.commands) == 2  # Never return onto a replacement/uncertain session.
        assert int(mirror.commands[1].split()[2]) < 0
    else:
        result = asyncio.run(operation)
        if interrupt == "no_landing":
            assert result is None
            assert not phone._pointer_pair_verified and phone._pos is None
        else:
            assert result == pytest.approx((296 / 500, 758 / 1080), abs=.003)
            assert phone._pointer_pair_verified
        assert len(mirror.commands) == 3
        moves = [int(command.split()[2]) for command in mirror.commands]
        assert moves[0] == -40 and moves[1] < 0 and sum(moves) == 0
        assert abs(sum(moves[:2])) <= .18 * 500


@pytest.mark.parametrize("near,axis,units", [((.5, .5), 0, 40), ((.5, .5), 1, -40),
                                          ((.5, .5), 0, 80), ((.5, .5), 1, -170),
                                          ((.95, .5), 0, 10), ((.5, .03), 1, -10)])
def test_extension_keeps_total_travel_and_screen_edges_bounded(tmp_path, near, axis, units):
    phone, _, _ = setup_phone(tmp_path)
    width, height = 360, 780
    extended = phone._extend_escape_probe(near, axis, units)
    if extended is not None:
        size = (width, height)[axis]
        position = near[axis] * size + extended * size / phone.span['wh'[axis]]
        assert .055 * width <= position <= size - .055 * width
        assert abs(extended) * size / phone.span['wh'[axis]] <= .18 * width
