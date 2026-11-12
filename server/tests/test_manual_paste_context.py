"""Manual Shortcut alerts can outlive the copy/focus acknowledgement in the mirror."""

import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fake_clock import ManualClock
from test_control_input_context import Link
from test_controls import jpeg

from hidagent import clipboard as clipboard_module
from hidagent import phone as phone_module
from hidagent.app import ControlAction
from hidagent.clipboard import CompanionClipboard
from hidagent.control import Controls
from hidagent.device import Device, DeviceError
from hidagent.phone import Phone
from hidagent.screen import Screen


def test_manual_paste_waits_for_delayed_original_context(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        original = jpeg()
        screen._on_jpeg(original)
        device.attach(link)
        clipboard = CompanionClipboard("http://clipboard.test", "x" * 40)
        phone = Phone(device, tmp_path / "cal.json", screen, clipboard=clipboard)
        controls = Controls(phone, screen)
        monkeypatch.setattr(controls, "_respond", respond)

        async def one_paste(text):
            request = ControlAction(action="type", text=text, mode="paste",
                                    screen_id=controls.observation()["screen_id"])
            task = asyncio.create_task(controls.perform(request))
            try:
                async def run_shortcut():
                    while clipboard.pending() is None:
                        if task.done():
                            raise AssertionError(await task)
                        await asyncio.sleep(.005)
                    token = clipboard.pending()["text_url"].rsplit("/", 1)[1]
                    assert clipboard.fetch(token) == text
                    screen._on_jpeg(jpeg("black"))  # dismissed alert still visible in the mirror
                    clipboard.acknowledge(token, copied=True, focused=True)
                    confirmed = time.monotonic()
                    restored = asyncio.Event()

                    def restore_original():
                        assert not task.done()
                        assert clipboard.status(token)["paste_sent"] is False
                        screen._on_jpeg(original)
                        restored.set()

                    clock.call_later(.8, restore_original)
                    while not clipboard.status(token)["paste_sent"]:
                        if task.done():
                            raise AssertionError(await task)
                        await asyncio.sleep(.005)
                    assert restored.is_set()
                    assert time.monotonic() - confirmed >= .8
                    clipboard.complete(token, pasted=True)

                await asyncio.wait_for(run_shortcut(), 2)
                result = await asyncio.wait_for(task, 1)
                assert result == {"command_acknowledged": True, "error": None}
                assert clipboard.current() == {"active": False, "needs_resolution": False}
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            await one_paste("第一条草稿")
            await one_paste("第二条草稿")
            assert [line.split(b" ", 1)[1].strip() for line in link.written] == [
                b"key cmd+v", b"key cmd+v"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["wrong_screen", "generation", "stale", "disconnect", "stop", "expiry"])
def test_manual_paste_refuses_ineligible_or_expired_context(tmp_path, monkeypatch, transition):
    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        original = jpeg()
        screen._on_jpeg(original)
        device.attach(link)
        clipboard = CompanionClipboard("http://clipboard.test", "x" * 40, ttl=1)
        phone = Phone(device, tmp_path / "cal.json", screen, clipboard=clipboard)
        controls = Controls(phone, screen)
        monkeypatch.setattr(controls, "_respond", respond)
        monkeypatch.setattr(phone_module, "PASTE_CONTEXT_WAIT", 3.5 if transition == "expiry" else .15)
        request = ControlAction(action="type", text="未发送草稿", mode="paste",
                                screen_id=controls.observation()["screen_id"])
        task = asyncio.create_task(controls.perform(request))
        try:
            async def pending():
                while clipboard.pending() is None:
                    await asyncio.sleep(.005)
                return clipboard.pending()["text_url"].rsplit("/", 1)[1]

            token = await asyncio.wait_for(pending(), 1)
            clipboard.fetch(token)
            screen._on_jpeg(jpeg("black"))
            clipboard.acknowledge(token, copied=True, focused=True)
            # A copy acknowledgement does not authorize a new generation, stale
            # image, disconnected transport, or a changed destination.
            if transition == "generation":
                screen._on_ready()
                screen._on_jpeg(original)
            elif transition == "stale":
                screen._on_jpeg(original)
                aged = time.time() - 10
                snap = screen.snapshot
                screen._snapshot = type(snap)(
                    snap.frame, snap.board_ts, snap.arrived, aged, snap.generation, snap.observed_live)
                with screen._frame_lock:
                    screen.updated = aged
            elif transition == "disconnect":
                screen.stop()
            elif transition == "stop":
                device.request_abort()
            result = await asyncio.wait_for(task, 2)
            assert result["command_acknowledged"] is False
            assert result["error"]
            assert link.written == []
            assert clipboard.current() == {"active": False, "needs_resolution": True}
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


def test_restored_manual_paste_revalidates_after_device_lock_wait(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        original = jpeg()
        screen._on_jpeg(original)
        device.attach(link)
        clipboard = CompanionClipboard("http://clipboard.test", "x" * 40)
        phone = Phone(device, tmp_path / "cal.json", screen, clipboard=clipboard)
        controls = Controls(phone, screen)
        monkeypatch.setattr(controls, "_respond", respond)
        request = ControlAction(action="type", text="未发送草稿", mode="paste",
                                screen_id=controls.observation()["screen_id"])
        await device._lock.acquire()
        task = asyncio.create_task(controls.perform(request))
        try:
            async def pending():
                while clipboard.pending() is None:
                    await asyncio.sleep(.005)
                return clipboard.pending()["text_url"].rsplit("/", 1)[1]

            token = await asyncio.wait_for(pending(), 1)
            clipboard.fetch(token)
            screen._on_jpeg(jpeg("black"))
            clipboard.acknowledge(token, copied=True, focused=True)
            restored = asyncio.Event()

            def restore_original():
                assert not task.done()
                screen._on_jpeg(original)
                restored.set()

            clock.call_later(.8, restore_original)

            async def waiting():
                while not device._lock._waiters:
                    if task.done():
                        raise AssertionError(await task)
                    await asyncio.sleep(.005)

            await asyncio.wait_for(waiting(), 1)
            assert restored.is_set()
            screen._on_jpeg(jpeg("black"))
            device._lock.release()
            result = await asyncio.wait_for(task, 1)
            assert result["command_acknowledged"] is False
            assert "changed" in result["error"]
            assert link.written == []
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["observed_manual", "direct_manual", "observed_triggered"])
def test_paste_expiring_during_final_validation_never_writes(tmp_path, monkeypatch, route):
    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        trigger = "ctrl+alt+p" if route == "observed_triggered" else None
        clipboard = CompanionClipboard("http://clipboard.test", "x" * 40, ttl=1, trigger_key=trigger)
        phone = Phone(device, tmp_path / "cal.json", screen, clipboard=clipboard)
        controls = Controls(phone, screen)
        clock = [0.0]
        monkeypatch.setattr(clipboard_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        original_validate = controls._validate_context

        def validate(plan, *, stage="context"):
            original_validate(plan, stage=stage)
            if route == "observed_manual" and stage != "accept" and screen._frame_lock._is_owned():
                # The deadline passes inside synchronous final image validation,
                # before asyncio's timeout callback can run.
                clock[0] = 2.0

        @contextmanager
        def gate(command):
            if route != "observed_manual" and command == "key cmd+v":
                clock[0] = 2.0
            yield

        async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
            return {"command_acknowledged": acknowledged, "error": error}

        monkeypatch.setattr(controls, "_validate_context", validate)
        monkeypatch.setattr(controls, "_respond", respond)
        device.gate = gate
        if route == "direct_manual":
            task = asyncio.create_task(phone.type("未发送草稿", mode="paste"))
        else:
            request = ControlAction(action="type", text="未发送草稿", mode="paste",
                                    screen_id=controls.observation()["screen_id"])
            task = asyncio.create_task(controls.perform(request))
        try:
            async def pending():
                while clipboard.pending() is None:
                    await asyncio.sleep(.005)
                return clipboard.pending()["text_url"].rsplit("/", 1)[1]

            token = await asyncio.wait_for(pending(), 1)
            clipboard.fetch(token)
            clipboard.acknowledge(token, copied=True, focused=True)
            if route == "direct_manual":
                with pytest.raises(DeviceError):
                    await asyncio.wait_for(task, 2)
            else:
                result = await asyncio.wait_for(task, 2)
                assert result["command_acknowledged"] is False
                assert result["error"]
            commands = [line.split(b" ", 1)[1].strip() for line in link.written]
            assert commands == ([b"key ctrl+alt+p"] if trigger else [])
            assert clipboard.current() == {"active": False, "needs_resolution": True}
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())
