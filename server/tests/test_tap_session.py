"""Legacy/direct contacts retain the measured pointer's original device session."""

import asyncio

import pytest
from test_control_input_context import Link
from test_controls import jpeg

from hidagent.device import Device, DeviceError, input_attempt
from hidagent.freshness import require_fresh
from hidagent.phone import Phone
from hidagent.screen import Screen


def rig(tmp_path, monkeypatch):
    screen, device, link = Screen(), Device(), Link()
    device.attach(link)
    screen._on_jpeg(jpeg())
    phone = Phone(device, tmp_path / 'calibration.json', screen)

    async def measured(x, y, **kwargs):
        phone._pointer_confirmed = True
        return x, y

    async def effect(*args, **kwargs):
        return False, True

    monkeypatch.setattr(phone, 'point_verified', measured)
    monkeypatch.setattr(phone, '_tap_effect', effect)
    return phone, device, link, measured


@pytest.mark.parametrize('change', ['reattach', 'device', 'abort'])
@pytest.mark.parametrize('contact', ['click', 'press'])
def test_direct_contact_rechecks_device_session_under_write_lock(tmp_path, monkeypatch, change, contact):
    async def scenario():
        phone, device, original, measured = rig(tmp_path, monkeypatch)
        replacement = Link()

        async def waiting_point(*args, **kwargs):
            result = await measured(*args, **kwargs)
            await device._lock.acquire()
            return result

        monkeypatch.setattr(phone, 'point_verified', waiting_point)
        action = (phone.tap(.5, .5) if contact == 'click'
                  else phone.tap_verified(.5, .5, duration=.3))
        task = asyncio.create_task(action)
        while not device._lock._waiters:
            await asyncio.sleep(0)
        if change == 'reattach':
            device.attach(replacement)
        elif change == 'device':
            other = Device()
            other.attach(replacement)  # same numeric link generation is insufficient
            phone.device = other
        else:
            device._abort_epoch += 1  # a cleared stop still invalidates queued work
        device._lock.release()
        try:
            with pytest.raises(DeviceError, match='连接已变化|已停止'):
                await task
            commands = original.written + replacement.written
            assert not any(row.split()[1] in {b'click', b'press'} for row in commands)
            if contact == 'press':
                assert any(row.split()[1] == b'release' for row in commands)
        finally:
            device.close()
            phone.device.close()

    asyncio.run(scenario())


def test_contact_does_not_reanchor_after_observation_await(tmp_path, monkeypatch):
    async def scenario():
        phone, device, original, _ = rig(tmp_path, monkeypatch)
        replacement = Link()
        ensure = phone.ensure_observation
        positions = []

        async def reconnecting_observation():
            result = await ensure()
            device.attach(replacement)
            return result

        async def measured(x, y, **kwargs):
            positions.append((x, y))
            phone._pointer_confirmed = True
            return x, y

        monkeypatch.setattr(phone, 'ensure_observation', reconnecting_observation)
        monkeypatch.setattr(phone, 'point_verified', measured)
        try:
            with pytest.raises(DeviceError, match='连接已变化'):
                await phone.tap(.5, .5)
            assert not positions
            assert not original.written and not replacement.written
        finally:
            device.close()

    asyncio.run(scenario())


def test_double_click_does_not_cross_reconnect_after_first_ack(tmp_path, monkeypatch):
    async def scenario():
        phone, device, original, _ = rig(tmp_path, monkeypatch)
        replacement = Link()
        send = phone._send

        async def reconnect_after_ack(epoch, command, **kwargs):
            result = await send(epoch, command, **kwargs)
            if command == 'click':
                device.attach(replacement)
            return result

        monkeypatch.setattr(phone, '_send', reconnect_after_ack)
        try:
            with pytest.raises(DeviceError, match='连接已变化'):
                await phone.tap_verified(.5, .5, click_count=2)
            assert len(original.written) == 1 and b' click\n' in original.written[0]
            assert not replacement.written
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['reattach', 'generation'])
def test_first_positioning_write_retains_original_contact_session(tmp_path, monkeypatch, change):
    async def scenario():
        screen, device, original, replacement = Screen(), Device(), Link(), Link()
        device.attach(original)
        screen._on_jpeg(jpeg())
        phone = Phone(device, tmp_path / 'calibration.json', screen)
        phone.span = {'w': 1000, 'h': 1000}
        phone._pace, phone._pos, phone._link = 3, (0, 0), device.links

        async def observed():
            return require_fresh(screen)

        monkeypatch.setattr(phone, 'ensure_observation', observed)
        await device._lock.acquire()
        task = asyncio.create_task(phone.tap(.5, .5))
        while not device._lock._waiters:
            await asyncio.sleep(0)
        if change == 'reattach':
            device.attach(replacement)
        else:
            screen._on_ready()
            screen._on_jpeg(jpeg())
        device._lock.release()
        try:
            with pytest.raises(RuntimeError, match='连接已变化'):
                await task
            assert not original.written and not replacement.written
        finally:
            device.close()

    asyncio.run(scenario())


def test_contact_preserves_outer_write_guards_and_immediate_attempt_state(tmp_path, monkeypatch):
    async def scenario():
        phone, device, link, _ = rig(tmp_path, monkeypatch)
        checked = []

        async def measured(x, y, **kwargs):
            await phone._send(device.abort_epoch, 'move 1 0')
            assert attempt.started and not attempt.written
            phone._pointer_confirmed = True
            return x, y

        async def effect(*args, **kwargs):
            assert attempt.started and attempt.written
            return False, True

        monkeypatch.setattr(phone, 'point_verified', measured)
        monkeypatch.setattr(phone, '_tap_effect', effect)
        try:
            with input_attempt(before_write=lambda: checked.append(len(link.written))) as attempt:
                await phone.tap(.5, .5)
            assert checked == [0, 1]
        finally:
            device.close()

    asyncio.run(scenario())


def test_contact_keeps_parent_rejection_and_restores_parent_scope(tmp_path, monkeypatch):
    async def scenario():
        phone, device, link, _ = rig(tmp_path, monkeypatch)
        blocked = True

        def original_guard():
            if blocked:
                raise DeviceError('original action rejected')

        try:
            with input_attempt(before_write=original_guard) as attempt:
                with pytest.raises(DeviceError, match='original action rejected'):
                    await phone.tap(.5, .5)
                assert not link.written and not attempt.started and not attempt.written
                blocked = False
                phone.screen._on_ready()  # the completed contact's guard must not leak out
                await device.send('move 1 0')
                assert attempt.started and not attempt.written
        finally:
            device.close()

    asyncio.run(scenario())
