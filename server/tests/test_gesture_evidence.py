"""Gesture guards share measured cursor evidence without hiding scene edits."""

import asyncio
import io
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.phone import Phone

BASE = (Path(__file__).parent / 'fixtures' / 'ios26' / 'keypad_no_disc.jpg').read_bytes()
SOURCE, TARGET = (.72, .67), (.50, .44)


def image_at(point, changed=False):
    with Image.open(io.BytesIO(BASE)) as source:
        frame = source.convert('RGBA')
    w, h = frame.size
    overlay = Image.new('RGBA', frame.size)
    x, y = point[0] * w, point[1] * h
    r = .025 * w
    # Colored/translucent pointer over a real keypad does not look like a gray disc.
    ImageDraw.Draw(overlay).ellipse((x-r, y-r, x+r, y+r), fill=(80, 110, 150, 145))
    frame = Image.alpha_composite(frame, overlay).convert('RGB')
    if changed:
        ImageDraw.Draw(frame).rectangle((.13*w, .32*h, .19*w, .35*h), fill='black')
    result = io.BytesIO()
    frame.save(result, 'JPEG', quality=90)
    return result.getvalue()


class Screen:
    generation = 1
    live = True

    def __init__(self):
        self.frame = image_at(SOURCE)
        self.updated = time.time()

    async def fresh(self, since, timeout):
        return None


class Device:
    links = 0
    abort_epoch = 0
    pace_version = 4

    def __init__(self):
        self.commands = []
        self.before_write = None

    async def send(self, command):
        self.commands.append(command)

    async def send_guarded(self, command, validate, **kwargs):
        if self.before_write:
            self.before_write(command)
        validate()
        await self.send(command)


@pytest.mark.parametrize('action', ['path', 'swipe', 'scroll'])
@pytest.mark.parametrize('change', ['none', 'scene', 'cursor_patch', 'generation'])
def test_guarded_gesture_accepts_only_measured_cursor_motion(tmp_path, monkeypatch, action, change):
    screen, device = Screen(), Device()
    original = screen.frame
    phone = Phone(device, tmp_path / 'calibration.json', screen)
    phone._pace = 4
    phone.span = {'w': 360, 'h': 780}
    phone._pos = (0, 0)
    phone._link = 0
    controls = Controls(phone, screen)
    observation = controls.observation()
    request = ControlAction(action=action, screen_id=observation['screen_id'],
                            points=[TARGET, (.60, .50)], x1=TARGET[0], y1=TARGET[1],
                            x2=.60, y2=.50, x=TARGET[0], y=TARGET[1], lines=2, hold=False)
    plan = controls._plan(request, observation['width'], observation['height'])
    plan.update(observation=controls._observation, target_frame=original)
    verified = []

    async def measured(x, y, **kwargs):
        verified.append((x, y))
        screen.frame = image_at(TARGET, changed=change == 'scene')
        screen.updated = time.time()
        phone._position_completed = time.time()
        phone._pointer_confirmed = True
        phone._pointer_overlay = (TARGET, screen.frame)
        phone._pointer_reference = (SOURCE, original, screen.frame)
        phone._pos = (180, 343)
        return TARGET

    monkeypatch.setattr(phone, 'point_verified', measured)

    def change_at_write(command):
        if command in {'press', 'scroll 2 0'}:
            if change == 'cursor_patch':
                screen.frame = image_at((.48, .44))
            elif change == 'generation':
                screen.generation += 1

    device.before_write = change_at_write
    if change == 'none':
        asyncio.run(controls._execute(plan))
        assert ('scroll 2 0' if action == 'scroll' else 'press') in device.commands
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(controls._execute(plan))
        assert 'press' not in device.commands
        assert 'scroll 2 0' not in device.commands
    assert verified == [TARGET]


@pytest.mark.parametrize('action', ['swipe', 'scroll'])
def test_public_gesture_refuses_unmeasured_start(tmp_path, monkeypatch, action):
    screen, device = Screen(), Device()
    phone = Phone(device, tmp_path / 'calibration.json', screen)
    phone.span = {'w': 360, 'h': 780}
    phone._pace = 4
    measured = []

    async def unseen(*args, **kwargs):
        measured.append(True)
        phone._pointer_confirmed = False
        return TARGET

    monkeypatch.setattr(phone, 'point_verified', unseen)
    operation = phone.swipe(*TARGET, .60, .50) if action == 'swipe' else phone.scroll(*TARGET, 2)
    with pytest.raises(RuntimeError):
        asyncio.run(operation)
    assert not any(command in {'press', 'scroll 2 0'} for command in device.commands)
    assert measured == [True]
