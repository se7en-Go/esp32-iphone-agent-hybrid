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


@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize("command", ["tap 200 350", "tap_text 通用"])
@pytest.mark.parametrize("intervening", [None, "find_text", "replan"])
def test_full_tap_feedback_does_not_disable_duplicate_suppression(monkeypatch, fresh, command, intervening):
    agent, phone, screen = setup()
    replies = [f"DO: {command}"]
    if intervening == "find_text":
        replies.append("DO: find_text 通用")
    replies += [f"DO: {command}", "DO: done stop"]
    agent.actor = Model(replies)
    agent.planner = Model(["Inspect the previous action's result"])
    monkeypatch.setattr(module.ocr, "find_matches", lambda *_, **__: [match()])

    async def tap(*point, **_kwargs):
        phone.calls.append(("tap", *point))
        result = tap_result(screen)
        if not fresh:
            result.update(fresh=False, screen_changed=None, effect_observed=None,
                          _after_frame=None, _after_snapshot=None)
        return result

    monkeypatch.setattr(phone, "tap_verified", tap)

    async def scenario():
        await agent._step()
        assert agent.steps[-1]["changed"] is (False if fresh else None)
        # A new observation can still contain the same screenshot; it does not
        # establish that the first tap failed or authorize a duplicate write.
        screen.updated = time.time()
        if intervening == "find_text":
            await agent._step()
        elif intervening == "replan":
            await agent._replan(stuck=True)
        await agent._step()

    asyncio.run(scenario())
    assert phone.calls == [("tap", .2, .35)]
    assert ("changed nothing" if fresh else "outcome is unknown") in agent.actor.prompts[-1]


def test_action_identity_uses_arguments_not_result_fields():
    agent, _, screen = setup()
    action = {"action": "tap", "x": 200, "y": 350, "precise": True}
    recorded = {**action, **tap_result(screen), "changed": False, "thought": "again"}
    assert _key(recorded) == {"action": "tap", "x": 200, "y": 350}
    assert _key(recorded) == _key({"action": "tap", "x": 200, "y": 350})
    agent.steps = [recorded, recorded]
    assert agent._repeating(action)
    assert not agent._repeating({**action, "x": 201})


def test_unknown_tap_cannot_repeat_after_queued_wait(monkeypatch):
    agent, phone, screen = setup()
    agent.actor = Model(["DO: tap 200 350", "DO: wait\nDO: tap 200 350"])

    async def tap(*point, **_kwargs):
        phone.calls.append(("tap", *point))
        return {**tap_result(screen), "fresh": False, "_after_frame": None, "_after_snapshot": None}

    monkeypatch.setattr(phone, "tap_verified", tap)

    async def scenario():
        await agent._step()
        with pytest.raises(ValueError, match="repeat"):
            await agent._step()

    asyncio.run(scenario())
    assert phone.calls == [("tap", .2, .35)]


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
    from input_scene import input_frame

    from hidagent.phone import Phone

    agent, _, screen = setup()
    if mode == "chunks":
        screen.frame = input_frame()
    writes = []
    original = screen.frame

    async def send(command):
        writes.append(command)
        screen.frame = input_frame(typed=True) if mode == "chunks" else frame("black")
        screen.updated = time.time()
        if mode == "clipboard" and command == "key ctrl+alt+p":
            screen.frame = original  # the Shortcut alert has left; Cmd+V still needs this field
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


class GateLock(asyncio.Lock):
    """The second acquire waits until the test has changed the screen."""

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


def test_paste_does_not_send_when_the_original_field_does_not_return(tmp_path, monkeypatch):
    from hidagent.device import DeviceError
    from hidagent.phone import Phone

    agent, _, screen = setup()
    writes = []

    async def send(command):
        writes.append(command)
        screen.frame = frame("black")
        screen.updated = time.time()

    async def paste(text, *, stopped, send):
        await send("key ctrl+alt+p")
        await send("key cmd+v")

    monkeypatch.setattr("hidagent.phone.PASTE_CONTEXT_WAIT", 0.05)
    agent.phone = Phone(SimpleNamespace(send=send), tmp_path / "calibration.json", screen,
                        clipboard=SimpleNamespace(paste=paste))
    action = {"action": "type", "text": "中文", "_shot": screen.frame, "_observation": snapshot(screen)}
    with pytest.raises(DeviceError, match="没有粘贴"):
        asyncio.run(agent._do(action))
    assert writes == ["key ctrl+alt+p"]


@pytest.mark.parametrize("transition", ["content", "generation", "disconnect"])
def test_later_type_chunk_rechecks_context_while_waiting_for_the_device_lock(tmp_path, transition):
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
        device._lock = GateLock()
        agent.phone = Phone(device, tmp_path / "calibration.json", screen)
        action = {"action": "type", "text": "x" * 600, "_shot": screen.frame,
                  "_observation": snapshot(screen)}
        task = asyncio.create_task(agent._do(action))
        await asyncio.wait_for(device._lock.waiting.wait(), 1)
        if transition == "content":
            screen.frame = frame("red")
        elif transition == "generation":
            screen.generation += 1
        else:
            screen.live = False
        screen.updated = time.time()
        device._lock.allow.set()
        with pytest.raises((ValueError, module.BlindError), match="changed|lost the phone screen"):
            await task
        assert len(link.written) == 1
        device.close()

    asyncio.run(scenario())


def test_highlight_fade_does_not_count_as_tap_progress(monkeypatch):
    from test_verified_tap import keypad_edit

    agent, phone, screen = setup()
    before, after = keypad_edit(), keypad_edit(highlight=True)
    screen.frame = before
    screen.updated = time.time()
    agent.actor = Model(["DO: tap 200 350", "DO: tap 200 350", "DO: done"])

    async def tap(*_point, **_kwargs):
        phone.calls.append("click")
        return {**tap_result(screen), "fresh": True, "screen_changed": False,
                "_after_frame": after, "_after_snapshot": snapshot(screen)}

    monkeypatch.setattr(phone, "tap_verified", tap)

    async def scenario():
        await agent._step()
        await agent._step()

    asyncio.run(scenario())
    assert phone.calls == ["click"]
    taps = [step for step in agent.steps if step["action"] == "tap"]
    assert len(taps) == 1 and taps[0]["changed"] is False
    assert module.vision.changed(before, after) is True


def test_cmd_v_rechecks_the_original_field_inside_the_device_lock(tmp_path):
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

    async def paste(text, *, stopped, send):
        await send("key ctrl+alt+p")
        await send("key cmd+v")

    async def scenario():
        agent, _, screen = setup()
        device, link = Device(), Link()
        device.attach(link)
        device._lock = GateLock()
        agent.phone = Phone(device, tmp_path / "calibration.json", screen,
                            clipboard=SimpleNamespace(paste=paste))
        action = {"action": "type", "text": "中文", "_shot": screen.frame,
                  "_observation": snapshot(screen)}
        task = asyncio.create_task(agent._do(action))
        await asyncio.wait_for(device._lock.waiting.wait(), 1)
        screen.frame = frame("black")
        screen.updated = time.time()
        device._lock.allow.set()
        with pytest.raises(ValueError, match="changed"):
            await task
        assert len(link.written) == 1
        assert link.written[0].endswith(b" key ctrl+alt+p\n")
        device.close()

    asyncio.run(scenario())
