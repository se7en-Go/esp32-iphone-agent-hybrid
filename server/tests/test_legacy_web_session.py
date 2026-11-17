"""Old web endpoints must not dispatch delayed input onto a replacement session."""

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from input_scene import input_frame
from test_control_input_context import Link
from test_enter_context import keyboard_boxes

from hidagent import app as module
from hidagent.control import Controls
from hidagent.device import Device
from hidagent.guard import Guard
from hidagent.phone import Phone
from hidagent.screen import Screen


def setup(tmp_path, monkeypatch, clipboard=None):
    device, screen, original = Device(), Screen(), Link()
    device.attach(original)
    screen._on_jpeg(input_frame())
    phone = Phone(device, tmp_path / 'calibration.json', screen, clipboard=clipboard)
    controls = Controls(phone, screen)
    for name, value in (
        ('device', device), ('screen', screen), ('phone', phone), ('controls', controls),
        ('manual_lock', asyncio.Lock()), ('agent', SimpleNamespace(running=False)),
        ('guard', Guard(clock=lambda: datetime(2023, 11, 14, 12).timestamp())),
    ):
        monkeypatch.setattr(module, name, value)
    device.gate = module._hid_gate

    async def read(_frame):
        return keyboard_boxes('搜索')

    async def no_pace():
        pass

    async def no_pointer(*args, **kwargs):
        return None

    monkeypatch.setattr('hidagent.ocr.read_text_async', read)
    monkeypatch.setattr(phone, '_pull_pace', no_pace)
    monkeypatch.setattr(phone, '_find_pointer', no_pointer)
    return phone, device, screen, original, controls


@pytest.mark.parametrize('route', ['key', 'type', 'enter', 'calibrate'])
@pytest.mark.parametrize('transition', ['reattach', 'generation'])
def test_legacy_route_rechecks_session_at_serial_write(tmp_path, monkeypatch, route, transition):
    async def scenario():
        _phone, device, screen, original, controls = setup(tmp_path, monkeypatch)
        replacement = Link()
        path, body = {
            'key': ('/api/key', {'text': 'a'}),
            'type': ('/api/type', {'text': 'abc', 'mode': 'keys'}),
            'enter': ('/api/key', {'text': 'enter', 'enter_intent': 'navigation',
                                 'screen_id': controls.observation()['screen_id']}),
            'calibrate': ('/api/calibrate/start', {}),
        }[route]
        await device._lock.acquire()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                    base_url='http://test') as client:
            task = asyncio.create_task(client.post(path, json=body))
            try:
                async def queued():
                    while not device._lock._waiters:
                        if task.done():
                            raise AssertionError((await task).text)
                        await asyncio.sleep(0)

                await asyncio.wait_for(queued(), 1)
                if transition == 'reattach':
                    device.attach(replacement)
                else:
                    screen._on_ready()
                    screen._on_jpeg(input_frame())
                device._lock.release()
                result = await asyncio.wait_for(task, 1)
                assert result.status_code == 409, result.text
                assert not original.written and not replacement.written
            finally:
                if device._lock.locked():
                    device._lock.release()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                device.close()

    asyncio.run(scenario())


def test_late_clipboard_ack_cannot_paste_after_reconnect(tmp_path, monkeypatch):
    async def scenario():
        copied, proceed = asyncio.Event(), asyncio.Event()

        async def paste(_text, *, stopped, send):
            copied.set()
            await proceed.wait()
            await send('key cmd+v')

        _phone, device, _screen, original, _controls = setup(
            tmp_path, monkeypatch, SimpleNamespace(paste=paste))
        replacement = Link()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                    base_url='http://test') as client:
            task = asyncio.create_task(client.post('/api/type', json={'text': '中文', 'mode': 'paste'}))
            try:
                await asyncio.wait_for(copied.wait(), 1)
                device.attach(replacement)
                proceed.set()
                result = await asyncio.wait_for(task, 1)
                assert result.status_code == 409, result.text
                assert not original.written and not replacement.written
            finally:
                proceed.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('caller', ['legacy_key', 'direct_tap'])
def test_legacy_actual_write_holds_mirror_publication_lock(tmp_path, monkeypatch, caller):
    async def scenario():
        phone, device, screen, link, _controls = setup(tmp_path, monkeypatch)
        publishing, published = threading.Event(), threading.Event()
        threads = []
        write = link.write

        def replace_frame():
            publishing.set()
            screen._on_ready()
            screen._on_jpeg(input_frame())
            published.set()

        def guarded_write(data):
            if threads:
                write(data)
                return
            thread = threading.Thread(target=replace_frame)
            threads.append(thread)
            thread.start()
            assert publishing.wait(1)
            assert not published.wait(.03), 'mirror generation changed after validation but before write'
            write(data)

        link.write = guarded_write
        try:
            if caller == 'legacy_key':
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                            base_url='http://test') as client:
                    result = await client.post('/api/key', json={'text': 'a'})
                    assert result.status_code in {200, 409}
            else:
                async def measured(x, y, **kwargs):
                    await phone._send(device.abort_epoch, 'move 1 0')
                    phone._pointer_confirmed = True
                    return x, y

                async def effect(*args, **kwargs):
                    return False, True

                monkeypatch.setattr(phone, 'point_verified', measured)
                monkeypatch.setattr(phone, '_tap_effect', effect)
                try:
                    await phone.tap(.5, .5)
                except RuntimeError:
                    pass  # publication after the atomic write may invalidate the remaining action
            for thread in threads:
                thread.join(1)
            assert published.is_set() and link.written
        finally:
            for thread in threads:
                thread.join(1)
            device.close()

    asyncio.run(scenario())
