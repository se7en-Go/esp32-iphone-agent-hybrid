"""The verified BLE search shortcut belongs to Home, not arbitrary app screens."""

import asyncio

import pytest
from input_scene import input_frame
from test_assistive_home import frame as home_frame
from test_control_input_context import Link
from test_controls import jpeg

from hidagent import feedback, freshness, ocr
from hidagent.device import Device
from hidagent.phone import AppSearchError, Phone
from hidagent.screen import Screen


def setup(tmp_path, monkeypatch, *, on_home=False):
    monkeypatch.setenv("HIDAGENT_SPOTLIGHT_BLE_VERIFIED", "1")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
    device, link, screen = Device(), Link(), Screen()
    device.attach(link)
    device.board_status = {"hid": {"path": "ble"}}
    screen._on_jpeg(home_frame(home=True) if on_home else jpeg("black"))
    phone = Phone(device, tmp_path / "cal.json", screen)

    async def focused(*_args):
        screen._on_jpeg(input_frame())
        return screen.frame

    async def labels(_frame):
        return [ocr.TextBox("Search", (.15, .765, .27, .795), (.21, .78), .99),
                ocr.TextBox("Siri Suggestions", (.1, .3, .35, .34), (.22, .32), .98)]

    monkeypatch.setattr(feedback, "wait_after_action", focused)
    monkeypatch.setattr(ocr, "read_text_async", labels)
    return phone, device, link, screen


@pytest.mark.parametrize("on_home", [False, True])
def test_search_visually_confirms_home_and_consumes_original_callback_once(tmp_path, monkeypatch, on_home):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch, on_home=on_home)
        original, events = screen.frame, []

        def before():
            assert screen.frame == original
            events.append("original")

        async def home():
            events.append("home")
            screen._on_jpeg(home_frame(home=True))

        phone.go_home = home
        try:
            await phone.open_app_search("Freeform", before_action=before)
            assert events == (["original"] if on_home else ["original", "home"])
            assert link.written == [b"1 key cmd+space\n", b"2 type Freeform\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["failure", "not_home", "link", "generation", "abort"])
def test_home_failure_or_changed_original_session_never_sends_search_query(tmp_path, monkeypatch, transition):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch)

        async def home():
            if transition == "failure":
                raise RuntimeError("home not verified")
            screen._on_jpeg(home_frame(home=transition != "not_home"))
            if transition == "link":
                device.links += 1
            elif transition == "generation":
                screen._on_ready()
                screen._on_jpeg(home_frame(home=True))
            elif transition == "abort":
                device._abort_epoch += 1

        phone.go_home = home
        try:
            with pytest.raises(AppSearchError):
                await phone.open_app_search("Freeform")
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["link", "generation", "abort"])
def test_original_session_still_guards_query_after_spotlight_ocr(tmp_path, monkeypatch, transition):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch, on_home=True)
        labels = ocr.read_text_async

        async def changed(frame):
            result = await labels(frame)
            if transition == "link":
                device.links += 1
            elif transition == "generation":
                screen._on_ready()
                screen._on_jpeg(frame)
            else:
                device._abort_epoch += 1
            return result

        monkeypatch.setattr(ocr, "read_text_async", changed)
        try:
            with pytest.raises(AppSearchError):
                await phone.open_app_search("Freeform")
            assert link.written == [b"1 key cmd+space\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["content", "link", "generation", "abort"])
def test_home_shortcut_revalidates_at_device_write_after_waiting(tmp_path, monkeypatch, transition):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch)

        async def home():
            screen._on_jpeg(home_frame(home=True))
            await device._lock.acquire()

        phone.go_home = home
        operation = asyncio.create_task(phone.open_app_search("Freeform"))
        try:
            async def waiting():
                while not device._lock._waiters:
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            if transition == "content":
                screen._on_jpeg(jpeg("black"))
            elif transition == "link":
                device.links += 1
            elif transition == "generation":
                screen._on_ready()
                screen._on_jpeg(home_frame(home=True))
            else:
                device._abort_epoch += 1
            device._lock.release()
            with pytest.raises(AppSearchError):
                await operation
            assert link.written == []
        finally:
            if device._lock.locked():
                device._lock.release()
            if not operation.done():
                operation.cancel()
            device.close()

    asyncio.run(scenario())
