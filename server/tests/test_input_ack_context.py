"""A firmware ACK cannot turn a replacement screen into typing evidence."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from input_scene import input_frame
from test_control_input_context import Link

from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.device import Device
from hidagent.freshness import snapshot
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.mark.parametrize("caller", ["agent", "controls"])
@pytest.mark.parametrize("focused", [True, False])
@pytest.mark.parametrize("replacement", ["whole_screen", "outside_field", "field_only"])
def test_chunk_ack_preserves_original_screen_context(tmp_path, monkeypatch, caller, focused, replacement):
    from test_agent_text import setup
    from test_controls import jpeg

    async def scenario():
        screen, device = Screen(), Device()
        screen._on_jpeg(input_frame(focused=focused))

        class AckGate(Link):
            def __init__(self):
                super().__init__()
                self.first_write = asyncio.Event()
                self.allow_ack = asyncio.Event()

            def write(self, data):
                super().write(data)
                self.first_write.set()

            async def readline(self):
                await self.allow_ack.wait()
                return await super().readline()

        link = AckGate()
        device.attach(link)
        phone = Phone(device, tmp_path / "cal.json", screen)
        text = "x" * 600
        if caller == "agent":
            agent, _, _ = setup()
            agent.phone, agent.screen = phone, screen
            operation = agent._do({"action": "type", "text": text, "_shot": screen.frame,
                                   "_observation": snapshot(screen)})
        else:
            controls = Controls(phone, screen)

            async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
                return {"command_acknowledged": acknowledged, "error": error}

            monkeypatch.setattr(controls, "_respond", respond)
            operation = controls.perform(ControlAction(
                action="type", text=text, screen_id=controls.observation()["screen_id"]))
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(link.first_write.wait(), 1)
            screen._on_jpeg(jpeg("black") if replacement == "whole_screen" else
                            input_frame(typed=True, replaced=replacement == "outside_field", focused=focused))
            link.allow_ack.set()
            allowed = replacement == "field_only" and focused
            if caller == "agent" and not allowed:
                with pytest.raises(ValueError, match="changed"):
                    await task
            else:
                result = await task
                if caller == "controls":
                    assert result["command_acknowledged"] is allowed
            assert len(link.written) == (2 if allowed else 1)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("caller", ["agent", "controls"])
def test_later_chunk_allows_delayed_edits_only_in_original_input_field(tmp_path, monkeypatch, caller):
    from test_agent_context import GateLock
    from test_agent_text import setup

    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(input_frame())
        device.attach(link)
        device._lock = GateLock()
        phone = Phone(device, tmp_path / "cal.json", screen)
        if caller == "agent":
            agent, _, _ = setup()
            agent.phone, agent.screen = phone, screen
            operation = agent._do({"action": "type", "text": "x" * 600,
                                   "_shot": screen.frame, "_observation": snapshot(screen)})
        else:
            controls = Controls(phone, screen)

            async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
                return {"command_acknowledged": acknowledged, "error": error}

            monkeypatch.setattr(controls, "_respond", respond)
            operation = controls.perform(ControlAction(
                action="type", text="x" * 600, screen_id=controls.observation()["screen_id"]))
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(device._lock.waiting.wait(), 1)
            # The first chunk's visible result can arrive after its ACK.
            screen._on_jpeg(input_frame(typed=True))
            device._lock.allow.set()
            result = await task
            if caller == "controls":
                assert result["command_acknowledged"] is True
            assert len(link.written) == 2
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["content", "generation", "disconnect", "stale"])
@pytest.mark.parametrize("at_write", [False, True])
def test_direct_type_endpoint_rechecks_screen_after_copy_ack(tmp_path, monkeypatch, transition, at_write):
    from hidagent import app as module
    from hidagent.guard import Guard

    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(input_frame())
        device.attach(link)
        copied, proceed = asyncio.Event(), asyncio.Event()

        async def paste(_text, *, stopped, send):
            assert not stopped()
            copied.set()  # the companion has acknowledged copying and focus
            await proceed.wait()
            await send("key cmd+v")

        phone = Phone(device, tmp_path / "cal.json", screen,
                      clipboard=SimpleNamespace(paste=paste))
        for name, value in (("phone", phone), ("device", device), ("screen", screen),
                            ("controls", Controls(phone, screen)), ("guard", Guard()),
                            ("manual_lock", asyncio.Lock()), ("agent", SimpleNamespace(running=False))):
            monkeypatch.setattr(module, name, value)
        monkeypatch.setattr("hidagent.phone.PASTE_CONTEXT_WAIT", 0)
        if at_write:
            await device._lock.acquire()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                     base_url="http://test") as client:
            task = asyncio.create_task(client.post("/api/type", json={"text": "中文", "mode": "paste"}))
            try:
                await asyncio.wait_for(copied.wait(), 1)
                if at_write:
                    proceed.set()

                    async def waiting():
                        while not device._lock._waiters:
                            if task.done():
                                raise AssertionError(await task)
                            await asyncio.sleep(0)

                    await asyncio.wait_for(waiting(), 1)
                if transition == "content":
                    screen._on_jpeg(input_frame(replaced=True))
                elif transition == "generation":
                    screen._on_ready()
                    screen._on_jpeg(input_frame())
                elif transition == "disconnect":
                    screen.stop()
                else:
                    screen.updated -= 10
                    snap = screen.snapshot
                    screen._snapshot = type(snap)(snap.frame, snap.board_ts, snap.arrived,
                                                  screen.updated, snap.generation, snap.observed_live)
                proceed.set()
                if at_write:
                    device._lock.release()
                response = await asyncio.wait_for(task, 1)
                assert response.status_code == 409, response.text
                assert link.written == []
            finally:
                if device._lock.locked():
                    device._lock.release()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                device.close()

    asyncio.run(scenario())
