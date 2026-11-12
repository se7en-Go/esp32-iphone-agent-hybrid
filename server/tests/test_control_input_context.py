"""Observed Controls input must retain its evidence while waiting for HID locks."""

import asyncio

import pytest
from test_controls import jpeg

from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.device import Device
from hidagent.freshness import snapshot
from hidagent.phone import Phone
from hidagent.screen import Screen


class Link:
    def __init__(self):
        self.written = []
        self.replies = asyncio.Queue()

    def write(self, data):
        self.written.append(data)
        self.replies.put_nowait(data.split(b" ", 1)[0] + b" ok\n")

    async def readline(self):
        return await self.replies.get()

    def is_open(self):
        return True

    def close(self):
        pass


@pytest.mark.parametrize("action,fields", [
    ("type", {"text": "hello"}),
    ("type", {"text": "hello", "mode": "keys"}),
    ("compose_chinese", {"text": "你好", "input_mode": "pinyin"}),
    ("key", {"text": "enter"}),
])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("lock_name", ["phone", "device"])
@pytest.mark.parametrize("transition", ["content", "generation"])
def test_input_rechecks_original_context_at_write(tmp_path, monkeypatch, action, fields,
                                                 batch, lock_name, transition):
    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        controls = Controls(phone, screen)
        monkeypatch.setattr(controls, "_respond", respond)
        request = ControlAction(action=action, screen_id=controls.observation()["screen_id"], **fields)
        lock = phone._interaction_lock if lock_name == "phone" else device._lock
        await lock.acquire()
        operation = (controls.perform_batch(request.screen_id, [request]) if batch
                     else controls.perform(request))
        task = asyncio.create_task(operation)
        try:
            async def waiting():
                while not lock._waiters:
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            if transition == "content":
                screen._on_jpeg(jpeg("black"))
            else:
                screen._on_ready()
                screen._on_jpeg(jpeg())  # reconnect with identical image still invalidates the action
            # A concurrent new observation must not replace the pending action's snapshot.
            controls.observation()
            lock.release()
            result = await task
            assert link.written == []
            assert result["command_acknowledged"] is False
            assert "changed" in result["error"]
        finally:
            if lock.locked():
                lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("caller,action", [
    ("controls", "swipe"), ("controls", "scroll"), ("controls", "back"),
    ("agent", "swipe"), ("agent", "path"), ("agent", "circle"), ("agent", "back"),
])
@pytest.mark.parametrize("transition", ["content", "generation"])
def test_gesture_callers_keep_observation_while_waiting_for_phone(
        tmp_path, monkeypatch, caller, action, transition):
    from test_agent_text import setup

    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        phone.span = {"w": 1000, "h": 2000}
        phone._pos, phone._link = (0, 0), device.links
        if caller == "controls":
            controls = Controls(phone, screen)
            monkeypatch.setattr(controls, "_respond", respond)
            fields = {"swipe": {"x1": .2, "y1": .3, "x2": .4, "y2": .5},
                      "scroll": {"x": .2, "y": .3, "lines": 2}, "back": {}}[action]
            request = ControlAction(action=action, screen_id=controls.observation()["screen_id"], **fields)
            operation = controls.perform(request)
        else:
            agent, _, _ = setup()
            agent.phone, agent.screen = phone, screen
            fields = {"x1": 200, "y1": 300, "x2": 400, "y2": 500,
                      "points": [(200, 300), (400, 500)], "x": 500, "y": 500, "r": 100}
            operation = agent._do({"action": action, **fields, "_shot": screen.frame,
                                   "_observation": snapshot(screen)})
        lock = phone._interaction_lock
        await lock.acquire()
        task = asyncio.create_task(operation)
        try:
            async def waiting():
                while not lock._waiters:
                    if task.done():
                        await task
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            if transition == "content":
                screen._on_jpeg(jpeg("black"))
            else:
                screen._on_ready()
                screen._on_jpeg(jpeg())
            lock.release()
            if caller == "controls":
                result = await task
                assert result["command_acknowledged"] is False
                assert "changed" in result["error"]
            else:
                with pytest.raises((RuntimeError, ValueError), match="changed"):
                    await task
            assert link.written == []
        finally:
            if lock.locked():
                lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["chunks", "clipboard"])
def test_input_checks_start_but_allows_its_own_screen_changes(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace

    screen = Screen()
    screen._on_jpeg(jpeg())
    original = screen.frame
    writes = []

    async def send(command):
        writes.append(command)
        screen._on_jpeg(jpeg("black"))
        if mode == "clipboard" and command == "key ctrl+alt+p":
            screen._on_jpeg(original)

    async def paste(text, *, stopped, send):
        assert not stopped()
        await send("key ctrl+alt+p")
        await send("key cmd+v")

    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    clipboard = SimpleNamespace(paste=paste) if mode == "clipboard" else None
    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json", screen, clipboard=clipboard)
    controls = Controls(phone, screen)
    monkeypatch.setattr(controls, "_respond", respond)
    text = "中文" if clipboard else "x" * 600
    result = asyncio.run(controls.perform(ControlAction(
        action="type", text=text, screen_id=controls.observation()["screen_id"])))
    assert result["command_acknowledged"] is True
    assert result["error"] is None
    if clipboard:
        assert writes == ["key ctrl+alt+p", "key cmd+v"]
    else:
        assert len(writes) == 2
        assert "".join(command.removeprefix("type ") for command in writes) == text


def test_controls_do_not_paste_over_a_replaced_screen(tmp_path, monkeypatch):
    from types import SimpleNamespace

    screen = Screen()
    screen._on_jpeg(jpeg())
    writes = []

    async def send(command):
        writes.append(command)
        screen._on_jpeg(jpeg("black"))

    async def paste(text, *, stopped, send):
        await send("key ctrl+alt+p")
        await send("key cmd+v")

    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json", screen,
                  clipboard=SimpleNamespace(paste=paste))
    controls = Controls(phone, screen)
    monkeypatch.setattr(controls, "_respond", respond)
    monkeypatch.setattr("hidagent.phone.PASTE_CONTEXT_WAIT", 0.05)
    result = asyncio.run(controls.perform(ControlAction(
        action="type", text="中文", screen_id=controls.observation()["screen_id"])))
    assert result["command_acknowledged"] is False
    assert "没有粘贴" in result["error"]
    assert writes == ["key ctrl+alt+p"]


def test_controls_recheck_a_later_chunk_after_an_identical_reconnect(tmp_path, monkeypatch):
    screen, device, link = Screen(), Device(), Link()
    screen._on_jpeg(jpeg())
    device.attach(link)

    class Gate(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.acquires = 0
            self.waiting = asyncio.Event()
            self.allow = asyncio.Event()

        async def acquire(self):
            self.acquires += 1
            if self.acquires == 2:
                self.waiting.set()
                await self.allow.wait()
            return await super().acquire()

    device._lock = Gate()

    async def respond(_before, _completed, _began, _ready, acknowledged, error, *_args):
        return {"command_acknowledged": acknowledged, "error": error}

    async def scenario():
        phone = Phone(device, tmp_path / "calibration.json", screen)
        controls = Controls(phone, screen)
        monkeypatch.setattr(controls, "_respond", respond)
        task = asyncio.create_task(controls.perform(ControlAction(
            action="type", text="x" * 600, screen_id=controls.observation()["screen_id"])))
        await asyncio.wait_for(device._lock.waiting.wait(), 1)
        frame = screen.frame
        screen._on_ready()
        screen._on_jpeg(frame)
        device._lock.allow.set()
        result = await task
        assert result["command_acknowledged"] is False
        assert "changed" in result["error"]
        assert len(link.written) == 1
        device.close()

    asyncio.run(scenario())
