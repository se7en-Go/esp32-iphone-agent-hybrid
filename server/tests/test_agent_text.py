import asyncio
import io
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from hidagent import agent as module
from hidagent.agent import Agent, BlindError, _diff_feedback, _key, _pointer_path, parse_command
from hidagent.debug import Trace


def frame(color="white"):
    data = io.BytesIO()
    Image.new("RGB", (100, 200), color).save(data, "JPEG")
    return data.getvalue()


class Screen:
    live = True
    aspect = .5
    frame = frame()
    fresh_frame = True

    async def fresh(self, *_):
        return self.frame if self.fresh_frame else None

    async def settled(self):
        pass


class Phone:
    def __init__(self):
        self.calls = []

    async def tap_verified(self, *point, before_click=None):
        if before_click:
            before_click()
        self.calls.append(("verified", *point))

    async def tap(self, *point, **kwargs):
        self.calls.append(("tap", *point))

    async def type(self, text):
        self.calls.append(("type", text))


def setup():
    phone, screen = Phone(), Screen()
    return Agent(phone, screen, None, None, Trace(None)), phone, screen


def match():
    return SimpleNamespace(text="通用", box=(.1, .3, .3, .4), center=(.2, .35), conf=.98)


@pytest.mark.parametrize("name", ["find_text", "tap_text"])
def test_parse_ocr_actions_preserves_query(name):
    assert parse_command(f'{name} "Wi-Fi 设置 １２３"') == {"action": name, "text": "Wi-Fi 设置 １２３"}


@pytest.mark.parametrize("name", ["find_text", "tap_text"])
def test_text_actions_use_observed_frame_and_verified_tap(monkeypatch, name):
    agent, phone, screen = setup()
    calls = []

    def find(seen, query, exact=False):
        calls.append((seen, query))
        return match()

    monkeypatch.setattr(module.ocr, "find_text", find)
    action = {"action": name, "text": "通用", "_shot": screen.frame}
    asyncio.run(agent._do(action))
    assert calls == [(screen.frame, "通用")]
    assert action["text_match"]["center"] == (.2, .35)
    assert phone.calls == ([("verified", .2, .35)] if name == "tap_text" else [])
    if name == "tap_text":
        assert _pointer_path(action) == [(.2, .35)]
    assert _key(action) == {"action": name, "text": "通用"}


def test_missing_text_does_not_click(monkeypatch):
    agent, phone, screen = setup()
    monkeypatch.setattr(module.ocr, "find_text", lambda *_: None)
    action = {"action": "find_text", "text": "不存在", "_shot": screen.frame}
    asyncio.run(agent._do(action))
    assert action["text_match"] is None
    action["action"] = "tap_text"
    with pytest.raises(ValueError, match="未找到"):
        asyncio.run(agent._do(action))
    assert phone.calls == []


def test_text_tap_rechecks_target_after_real_pointer_positioning(monkeypatch, tmp_path):
    from hidagent.phone import Phone as RealPhone

    agent, _, screen = setup()
    commands = []

    async def send(command):
        commands.append(command)

    phone = RealPhone(SimpleNamespace(send=send), tmp_path / "calibration.json", screen)
    agent.phone = phone
    seen = screen.frame
    monkeypatch.setattr(module.ocr, "find_text", lambda *_: match())

    async def positioned(*_):
        screen.frame = frame("black")
        return match().center

    monkeypatch.setattr(phone, "point_verified", positioned)
    with pytest.raises((RuntimeError, ValueError), match="变化|changed"):
        asyncio.run(agent._do({"action": "tap_text", "text": "通用", "_shot": seen}))
    assert commands == []


@pytest.mark.parametrize("when", ["before", "during", "disconnected"])
def test_stale_ocr_result_never_reaches_hid(monkeypatch, when):
    agent, phone, screen = setup()
    action = {"action": "tap_text", "text": "通用", "_shot": screen.frame}

    def find(*_):
        if when == "disconnected":
            screen.live = False
        else:
            screen.frame = frame("black")
        return match()

    monkeypatch.setattr(module.ocr, "find_text", find)
    if when == "before":
        screen.frame = frame("black")
    with pytest.raises(BlindError if when == "disconnected" else ValueError):
        asyncio.run(agent._do(action))
    assert phone.calls == []


def test_cancel_during_ocr_keeps_event_loop_live_and_sends_no_hid(monkeypatch):
    agent, phone, screen = setup()
    entered, release = threading.Event(), threading.Event()

    def find(*_):
        entered.set()
        assert release.wait(2)
        return match()

    monkeypatch.setattr(module.ocr, "find_text", find)

    async def scenario():
        task = asyncio.create_task(agent._do({"action": "tap_text", "text": "通用", "_shot": screen.frame}))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            while not module.ocr._lookup.done.is_set():
                await asyncio.sleep(.001)

    asyncio.run(scenario())
    assert phone.calls == []


@pytest.mark.parametrize("name", ["tap", "tap_text"])
def test_every_tap_records_screen_diff_before_next_action(monkeypatch, name):
    agent, phone, screen = setup()
    before, after = screen.frame, frame("black")
    monkeypatch.setattr(module.ocr, "find_text", lambda *_: match())
    diff_calls = []

    async def summarize(a, b, *, fresh):
        diff_calls.append((a, b, fresh))
        screen.frame = before  # the stream may advance while optional OCR runs
        return {"changed_fraction": 1, "changed_regions": [[0, 0, 1, 1]], "text_status": "disabled"}

    async def tap(*_, **kwargs):
        screen.frame = after

    async def actions():
        return [{"action": name, "x": 200, "y": 350, "text": "通用", "_shot": before}]

    monkeypatch.setattr(module.screen_diff, "summarize", summarize)
    monkeypatch.setattr(phone, "tap_verified", tap)
    monkeypatch.setattr(phone, "tap", tap)
    monkeypatch.setattr(agent, "_next_actions", actions)
    assert asyncio.run(agent._step()) is False
    assert diff_calls == [(before, after, True)]
    assert agent.steps[-1]["screen_diff"]["changed_fraction"] == 1
    assert agent.steps[-1]["changed"] is True


@pytest.mark.parametrize("reason", ["stale", "disconnected"])
def test_tap_without_fresh_frame_does_not_continue_typing(monkeypatch, reason):
    agent, phone, screen = setup()
    screen.fresh_frame = reason != "stale"

    async def tap(*point, **kwargs):
        phone.calls.append(("tap", *point))
        if reason == "disconnected":
            screen.live = False

    async def summarize(*_, **kwargs):
        return {"status": "stale"}

    async def actions():
        return [{"action": "tap", "x": 200, "y": 350, "_shot": screen.frame},
                {"action": "type", "text": "hello", "_shot": screen.frame}]

    monkeypatch.setattr(module.screen_diff, "summarize", summarize)
    monkeypatch.setattr(agent, "_next_actions", actions)
    monkeypatch.setattr(phone, "tap", tap)
    asyncio.run(agent._step())
    assert phone.calls == [("tap", .2, .35)]
    assert agent.steps[-1]["changed"] is None


def test_find_text_returns_to_actor_before_following_action(monkeypatch):
    agent, phone, screen = setup()
    monkeypatch.setattr(module.ocr, "find_text", lambda *_: match())

    async def actions():
        return [{"action": "find_text", "text": "通用", "_shot": screen.frame},
                {"action": "type", "text": "hello", "_shot": screen.frame}]

    monkeypatch.setattr(agent, "_next_actions", actions)
    assert asyncio.run(agent._step()) is False
    assert phone.calls == []
    assert agent.steps[-1]["text_match"]["text"] == "通用"


@pytest.mark.parametrize("feedback", ["stale", "screen_changed"])
@pytest.mark.parametrize("following", ["tap", "swipe"])
def test_text_tap_discards_queued_coordinates(monkeypatch, feedback, following):
    agent, phone, screen = setup()
    seen = screen.frame
    screen.fresh_frame = feedback != "stale"
    monkeypatch.setattr(module.ocr, "find_text", lambda *_: match())

    async def tap_text(*point, **kwargs):
        phone.calls.append(("verified", *point))
        if feedback == "screen_changed":
            screen.frame = frame("black")

    async def swipe(*point):
        phone.calls.append(("swipe", *point))

    async def actions():
        return [{"action": "tap_text", "text": "通用", "_shot": seen},
                {"action": following, "x": 500, "y": 500,
                 "x1": 100, "y1": 100, "x2": 900, "y2": 900, "_shot": seen}]

    monkeypatch.setattr(phone, "tap_verified", tap_text)
    monkeypatch.setattr(phone, "swipe", swipe, raising=False)
    monkeypatch.setattr(agent, "_next_actions", actions)
    assert asyncio.run(agent._step()) is False
    assert phone.calls == [("verified", .2, .35)]
    assert len(agent.steps) == 1
    assert agent.steps[0]["changed"] is (None if feedback == "stale" else True)


@pytest.mark.parametrize("name", ["tap", "tap_text"])
def test_delayed_pointer_frame_is_not_tap_feedback(monkeypatch, name):
    agent, phone, screen = setup()
    seen = screen.frame
    clock = [100.0]
    since_values = []
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module.ocr, "find_text", lambda *_: match())

    async def tap(*point, **kwargs):
        # Positioning took five seconds; a pre-click frame then arrived one second late.
        screen.updated = clock[0] = 105.5
        return 105.0

    async def fresh(since, timeout):
        since_values.append(since)
        return screen.frame if screen.updated > since else None

    async def actions():
        return [{"action": name, "x": 200, "y": 350, "text": "通用", "_shot": seen}]

    monkeypatch.setattr(phone, "tap_verified", tap)
    monkeypatch.setattr(phone, "tap", tap)
    monkeypatch.setattr(screen, "fresh", fresh)
    monkeypatch.setattr(agent, "_next_actions", actions)
    assert asyncio.run(agent._step()) is False
    assert agent.steps[-1]["changed"] is None
    assert agent.steps[-1]["screen_diff"]["status"] == "stale"
    assert since_values == [pytest.approx(105.0 + module.feedback.MIRROR_LAG_SECONDS)]
    assert '"status": "stale"' in _diff_feedback(agent.steps[-1])


def test_actor_history_keeps_text_changes_when_diff_has_many_regions(monkeypatch):
    agent, _, screen = setup()
    diff = {"changed_regions": [[.1234, .2345, .3456, .4567]] * 12, "changed_fraction": .27,
            "text_status": "available", "texts_appeared": ["已连接", "a" * 70, "第三项", "第四项省略"],
            "texts_disappeared": ["正在连接"]}
    agent.steps = [{"action": "tap_text", "text": "连接", "changed": True, "screen_diff": diff}]
    prompts = []

    async def ask(_role, _model, _system, prompt, *_):
        prompts.append(prompt)
        return "DO: done connected", 0, screen.frame

    monkeypatch.setattr(agent, "_ask", ask)
    asyncio.run(agent._next_actions())
    prompt = prompts[0]
    assert '"changed_fraction": 0.27' in prompt and '"text_status": "available"' in prompt
    assert "已连接" in prompt and "正在连接" in prompt and "第三项" in prompt
    assert "changed_regions" not in prompt and "第四项省略" not in prompt and "a" * 61 not in prompt
    assert len(diff["changed_regions"]) == 12 and len(diff["texts_appeared"]) == 4
