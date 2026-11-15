"""Strict localization and per-stroke session continuity without physical input."""

import asyncio
import io
import time

import pytest
from PIL import Image

from hidagent.phone import Phone
from hidagent.text_target import PointerLandingError


class Mirror:
    live = True
    generation = 1

    def __init__(self):
        output = io.BytesIO()
        Image.new('RGB', (360, 780), 'white').save(output, 'PNG')
        self.frame = output.getvalue()
        self.updated = time.time()


class Device:
    links = 0
    abort_epoch = 0
    pace_version = 4

    def __init__(self):
        self.commands = []
        self.after_press = None
        self.before_move_write = None
        self.pressed = False

    async def send(self, command):
        self.commands.append(command)
        if command == 'press':
            self.pressed = True
            if self.after_press:
                self.after_press()
        elif command == 'release':
            self.pressed = False

    async def send_guarded(self, command, before_write, **kwargs):
        if self.pressed and command.startswith('move ') and self.before_move_write:
            self.before_move_write()
        before_write()
        await self.send(command)


def setup_phone(tmp_path):
    mirror, device = Mirror(), Device()
    phone = Phone(device, tmp_path / 'calibration.json', mirror)
    phone.span = {'w': 360, 'h': 780}
    phone._pos = (0, 0)
    phone._pace = 4
    phone._link = 0
    return phone, device, mirror


@pytest.mark.parametrize('change', ['link', 'generation', 'abort', 'mirror_lost'])
def test_stroke_stops_after_session_change_without_rehoming(tmp_path, change):
    phone, device, mirror = setup_phone(tmp_path)

    def interrupt():
        if change == 'link':
            device.links += 1
        elif change == 'generation':
            mirror.generation += 1
        elif change == 'abort':
            device.abort_epoch += 1
        else:
            mirror.live = False

    device.after_press = interrupt
    with pytest.raises(RuntimeError):
        asyncio.run(phone.drag([(.3, .3), (.4, .4), (.5, .5)], hold=False))
    assert device.commands == ['move 108 234', 'press', 'release']
    assert phone._pos is None


def test_stroke_rechecks_session_inside_device_write_lock(tmp_path):
    phone, device, mirror = setup_phone(tmp_path)
    device.before_move_write = lambda: setattr(mirror, 'generation', 2)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.drag([(.3, .3), (.4, .4)], hold=False))
    assert device.commands == ['move 108 234', 'press', 'release']
    assert phone._pos is None


@pytest.mark.parametrize('idle', [False, True])
def test_point_without_timing_localizes_hidden_pointer(tmp_path, monkeypatch, idle):
    phone, device, mirror = setup_phone(tmp_path)
    phone._pos = (180, 390) if idle else (0, 0)
    phone._last_motion = 0
    found = []

    async def no_visible_landing(*args):
        return None

    async def paired_landing(near, epoch, **kwargs):
        found.append(near)
        phone._pointer_pair_verified = True
        return near

    monkeypatch.setattr(phone, '_observe_landing', no_visible_landing)
    monkeypatch.setattr(phone, '_localize_pointer', paired_landing)
    assert phone._tap_timing is None
    asyncio.run(phone.point_verified(.5, .5))
    assert found == [(.5, .5)]
    assert phone._pointer_confirmed
    assert phone._tap_timing is None
    assert 'click' not in device.commands


def test_point_correction_without_timing_relocalizes(tmp_path, monkeypatch):
    phone, device, mirror = setup_phone(tmp_path)
    calls = []

    async def first(*args, **kwargs):
        phone._pointer_pair_verified = True
        return .53, .52

    async def invisible(*args):
        return None

    async def localized(near, epoch, **kwargs):
        calls.append(near)
        phone._pointer_pair_verified = True
        return near

    monkeypatch.setattr(phone, '_find_pointer', first)
    monkeypatch.setattr(phone, '_observe_landing', invisible)
    monkeypatch.setattr(phone, '_localize_pointer', localized)
    asyncio.run(phone.point_verified(.5, .5))
    assert calls == [(.5, .5)]
    assert phone._pointer_confirmed
    assert phone._tap_timing is None


def test_required_path_refuses_unknown_landing(tmp_path, monkeypatch):
    phone, device, mirror = setup_phone(tmp_path)

    async def unknown(*args, **kwargs):
        return None

    monkeypatch.setattr(phone, '_find_pointer', unknown)
    with pytest.raises(PointerLandingError):
        asyncio.run(phone.drag([(.3, .3), (.4, .4)], verify_start=True))
    assert 'press' not in device.commands
