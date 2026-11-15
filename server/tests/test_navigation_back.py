"""Back navigation preserves original observation/session guards through its visible target tap."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_navigation_vision import navigation_frame
from test_review3_gestures import Link

from hidagent.device import Device
from hidagent.phone import Phone
from hidagent.screen import Screen


def setup_phone(tmp_path):
    screen = Screen()
    screen._on_ready()
    screen._on_jpeg(navigation_frame())
    commands = []

    async def send(command):
        commands.append(command)

    device = SimpleNamespace(links=1, abort_epoch=0, send=send)
    phone = Phone(device, tmp_path / "cal.json", screen)

    async def measured(x, y, **kwargs):
        phone._pointer_confirmed = True
        return x, y

    async def effect(*_args):
        return False, True

    phone.point_verified, phone._tap_effect = measured, effect
    return phone, screen, device, commands


def test_visible_back_target_uses_guarded_click_not_edge_swipe(tmp_path):
    async def scenario():
        phone, _, _, commands = setup_phone(tmp_path)
        checks = []
        await phone.go_back(before_action=lambda **_evidence: checks.append(True))
        assert commands == ["click"]
        assert len(checks) >= 2
        assert phone.last_tap["effect_verified"] is False  # ACK never claims the page navigated.

    asyncio.run(scenario())


def test_back_entry_does_not_reuse_previous_pages_pointer_evidence(tmp_path):
    async def scenario():
        phone, screen, _, commands = setup_phone(tmp_path)
        with Image.open(io.BytesIO(screen.frame)) as image:
            previous_page = image.convert("RGB")
        ImageDraw.Draw(previous_page).rectangle((220, 780, 280, 840), fill="black")
        encoded = io.BytesIO()
        previous_page.save(encoded, "PNG")
        phone._pointer_confirmed = True
        phone._pointer_overlay = ((.5, .75), encoded.getvalue())
        evidence = []

        async def measured(x, y, **_kwargs):
            phone._pointer_confirmed = True
            phone._pointer_overlay = ((x, y), screen.frame)
            phone._pointer_reference = None
            return x, y

        phone.point_verified = measured
        await phone.go_back(before_action=lambda **kwargs: evidence.append(kwargs))
        assert commands == ["click"]
        assert evidence[0]["cursor"] is None
        assert evidence[-1]["cursor"] == phone._pointer_overlay

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["generation", "link", "stop", "caller_guard"])
def test_visible_back_never_reanchors_after_original_selection(tmp_path, transition):
    async def scenario():
        phone, screen, device, commands = setup_phone(tmp_path)
        selected = screen.frame
        original = phone.ensure_observation
        changed = False

        async def change_then_observe():
            nonlocal changed
            changed = True
            if transition == "generation":
                screen._on_ready()
                screen._on_jpeg(selected)
            elif transition == "link":
                device.links += 1
            elif transition == "stop":
                device.abort_epoch += 1
            return await original()

        def guard(**_evidence):
            if changed and transition == "caller_guard":
                raise RuntimeError("original caller context changed")

        phone.ensure_observation = change_then_observe
        with pytest.raises(RuntimeError):
            await phone.go_back(before_action=guard)
        assert "click" not in commands and "press" not in commands

    asyncio.run(scenario())


def test_unknown_navigation_retains_original_guarded_edge_gesture(tmp_path):
    async def scenario():
        phone, screen, _, commands = setup_phone(tmp_path)
        screen._on_jpeg(navigation_frame(arrow=None))
        attempts = []

        def guard():
            pass

        async def swipe(*args, **kwargs):
            attempts.append((args, kwargs))

        phone.swipe = swipe
        await phone.go_back(before_action=guard)
        assert attempts == [((.005, .45, .6, .45), {"hold": False, "before_action": guard})]
        assert not commands

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["content", "generation", "disconnect", "link", "stop"])
def test_visible_back_revalidates_after_waiting_for_actual_device_write(tmp_path, transition):
    async def scenario():
        phone, screen, _, _ = setup_phone(tmp_path)
        device, link = Device(), Link()
        device.attach(link)
        phone.device = device
        selected = screen.frame
        point = phone.point_verified

        async def measured_then_block(*args, **kwargs):
            landed = await point(*args, **kwargs)
            await device._lock.acquire()
            return landed

        phone.point_verified = measured_then_block
        task = asyncio.create_task(phone.go_back())
        try:
            async def waiting():
                while not device._lock._waiters:
                    await asyncio.sleep(0)
            await asyncio.wait_for(waiting(), 1)
            if transition == "content":
                screen._on_jpeg(navigation_frame(arrow=None))
            elif transition == "generation":
                screen._on_ready()
                screen._on_jpeg(selected)
            elif transition == "disconnect":
                screen._started, screen._session_ok = True, False
            elif transition == "link":
                device.links += 1
            else:
                device._abort_epoch += 1  # Isolate the epoch check without cancelling the test task.
            device._lock.release()
            with pytest.raises(RuntimeError):
                await task
            assert "click" not in link.written
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())
