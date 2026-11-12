import asyncio
import time
from types import SimpleNamespace

import pytest
from test_agent_text import frame, match, setup

from hidagent import agent as module
from hidagent.agent import _key
from hidagent.freshness import snapshot


class Model:
    model = "fake"

    def __init__(self, replies, during=None):
        self.replies = iter(replies)
        self.during = during
        self.prompts = []

    async def chat(self, _system, prompt, _image, **_kwargs):
        self.prompts.append(prompt)
        if self.during:
            self.during()
        return next(self.replies)


@pytest.mark.parametrize("name", ["tap_text", "tap", "type", "key", "swipe"])
@pytest.mark.parametrize("transition", ["content", "generation"])
def test_planned_actions_reject_changed_original_context(monkeypatch, name, transition):
    agent, phone, screen = setup()
    screen.generation = 0
    commands = {"tap_text": "tap_text 通用", "tap": "tap 200 350", "type": "type hello",
                "key": "key enter", "swipe": "swipe 100 100 200 200"}

    def change():
        if transition == "content":
            screen.frame = frame("black")
        else:
            screen.generation += 1
        screen.updated = time.time()

    async def key(combo, **kwargs):
        phone.calls.append(("key", combo))

    async def swipe(*points):
        phone.calls.append(("swipe", *points))

    phone.key, phone.swipe = key, swipe
    agent.actor = Model([f"DO: {commands[name]}"], change)
    monkeypatch.setattr(module.ocr, "find_matches", lambda *_, **__: [match()])

    async def scenario():
        actions = await agent._next_actions()
        with pytest.raises((RuntimeError, ValueError), match="changed|变化"):
            await agent._do(actions[0])

    asyncio.run(scenario())
    assert phone.calls == []


def test_identical_image_reconnect_during_ocr_does_not_click(monkeypatch):
    agent, phone, screen = setup()
    screen.generation = 0
    agent.actor = Model(["DO: tap_text 通用"])

    def read(*_, **__):
        screen.generation += 1
        screen.updated = time.time()
        return [match()]

    monkeypatch.setattr(module.ocr, "find_matches", read)

    async def scenario():
        actions = await agent._next_actions()
        with pytest.raises((RuntimeError, ValueError), match="changed|变化"):
            await agent._do(actions[0])

    asyncio.run(scenario())
    assert phone.calls == []


def test_changed_context_discards_queued_enter():
    agent, phone, screen = setup()
    agent.actor = Model(["DO: type hello\nDO: key enter\nDO: key tab"])

    async def type_text(text, **kwargs):
        phone.calls.append(("type", text))
        screen.frame = frame("black")
        screen.updated = time.time()

    async def key(combo, **kwargs):
        phone.calls.append(("key", combo))

    phone.type, phone.key = type_text, key
    with pytest.raises((RuntimeError, ValueError), match="changed|变化"):
        asyncio.run(agent._step())
    assert phone.calls == [("type", "hello")]


def tap_result(screen):
    return {"clicked": True, "pointer_seen": False, "effect_verified": False,
            "effect_observed": False, "screen_changed": False, "fresh": True,
            "completed_at": time.time(), "latency": .315,
            "timing_ms": {"positioning": 200, "firmware": 10, "wait_for_frames": 100, "diff": 5, "total": 315},
            "warning": "Effect unknown", "_after_frame": screen.frame, "_after_snapshot": snapshot(screen)}


def test_full_tap_feedback_does_not_disable_duplicate_suppression(monkeypatch):
    agent, phone, screen = setup()
    agent.actor = Model(["DO: tap 200 350", "DO: tap 200 350", "DO: done stop"])

    async def tap(*point, **_kwargs):
        phone.calls.append(("tap", *point))
        return tap_result(screen)

    monkeypatch.setattr(phone, "tap_verified", tap)

    async def scenario():
        await agent._step()
        assert agent.steps[-1]["changed"] is False
        await agent._step()

    asyncio.run(scenario())
    assert phone.calls == [("tap", .2, .35)]
    assert "changed nothing" in agent.actor.prompts[-1]


def test_action_identity_uses_arguments_not_result_fields():
    agent, _, screen = setup()
    action = {"action": "tap", "x": 200, "y": 350, "precise": True}
    recorded = {**action, **tap_result(screen), "changed": False, "thought": "again"}
    assert _key(recorded) == action
    agent.steps = [recorded, recorded]
    assert agent._repeating(action)
    assert not agent._repeating({**action, "x": 201})


@pytest.mark.parametrize("name", ["type", "key"])
@pytest.mark.parametrize("lock_name", ["phone", "device"])
@pytest.mark.parametrize("transition", ["content", "generation"])
def test_agent_rechecks_context_after_waiting_for_action_locks(tmp_path, name, lock_name, transition):
    from hidagent.device import Device
    from hidagent.phone import Phone

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

    async def scenario():
        agent, _, screen = setup()
        screen.generation = 0
        device, link = Device(), Link()
        device.attach(link)
        agent.phone = phone = Phone(device, tmp_path / "calibration.json", screen)
        action = {"action": name, "text": "hello", "combo": "enter",
                  "_shot": screen.frame, "_observation": snapshot(screen)}
        lock = phone._interaction_lock if lock_name == "phone" else device._lock
        await lock.acquire()
        task = asyncio.create_task(agent._do(action))
        try:
            async def waiting():
                while not lock._waiters:
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), timeout=1)
            assert not task.done()
            if transition == "content":
                screen.frame = frame("black")
            else:
                screen.generation += 1
            screen.updated = time.time()
            lock.release()
            with pytest.raises(ValueError, match="changed"):
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
def test_input_validates_start_without_rejecting_its_own_screen_changes(tmp_path, mode):
    from hidagent.phone import Phone

    agent, _, screen = setup()
    writes = []

    async def send(command):
        writes.append(command)
        screen.frame = frame("black")  # typing or opening the Shortcut changes the observed UI
        screen.updated = time.time()

    async def paste(text, *, stopped, send):
        assert not stopped()
        await send("key ctrl+alt+p")
        # Production clipboard verifies copy and target focus before proceeding.
        await send("key cmd+v")

    clipboard = SimpleNamespace(paste=paste) if mode == "clipboard" else None
    agent.phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json", screen, clipboard=clipboard)
    text = "中文" if clipboard else "x" * 600
    action = {"action": "type", "text": text, "_shot": screen.frame, "_observation": snapshot(screen)}
    asyncio.run(agent._do(action))
    if clipboard:
        assert writes == ["key ctrl+alt+p", "key cmd+v"]
    else:
        assert len(writes) == 2
        assert "".join(command.removeprefix("type ") for command in writes) == text
