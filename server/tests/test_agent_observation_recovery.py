import asyncio

from test_agent_context import Model
from test_control_input_context import Link
from test_controls import jpeg

from hidagent.agent import Agent
from hidagent.debug import Trace
from hidagent.device import Device
from hidagent.phone import Phone
from hidagent.screen import Screen


class Actor(Model):
    def __init__(self, replies, during=None):
        super().__init__(replies, during)
        self.images = []

    async def chat(self, system, prompt, image, **kwargs):
        self.images.append(image)
        return await super().chat(system, prompt, image, **kwargs)


async def run_agent(tmp_path, screen, actor, link=None):
    device, link = Device(), link or Link()
    device.attach(link)
    phone = Phone(device, tmp_path / "calibration.json", screen)
    planner = Model(["1. Use the current screen"] * 4)
    agent = Agent(phone, screen, actor, planner, Trace(None))
    try:
        await agent._run()
        return agent, link.written
    finally:
        device.close()


def test_preflight_change_discards_batch_and_asks_actor_once_on_new_frame(tmp_path):
    screen = Screen()
    original, updated = jpeg(), jpeg("black")
    screen._on_jpeg(original)

    def change_first_frame():
        if len(actor.prompts) == 1:
            screen._on_jpeg(updated)

    actor = Actor(["DO: key a\nDO: key x", "DO: key b", "DO: done"], change_first_frame)
    agent, written = asyncio.run(run_agent(tmp_path, screen, actor))

    assert written == [b"1 key b\n"]
    assert actor.images[:2] == [original, updated]
    assert len(agent.planner.prompts) == 1
    assert agent.steps[-1]["action"] == "done"


def test_second_preflight_change_stops_instead_of_repeating_recovery(tmp_path):
    screen = Screen()
    screen._on_jpeg(jpeg())

    def change_both_frames():
        if len(actor.prompts) <= 2:
            screen._on_jpeg(jpeg("black" if len(actor.prompts) == 1 else "white"))

    actor = Actor(["DO: key a", "DO: key b", "DO: key x", "DO: done"], change_both_frames)
    agent, written = asyncio.run(run_agent(tmp_path, screen, actor))

    assert written == []
    assert len(actor.prompts) == 2 and len(agent.planner.prompts) == 1
    assert agent.steps[-1]["action"] == "fail"


def test_actor_cannot_replan_from_the_recovery_observation(tmp_path):
    screen = Screen()
    screen._on_jpeg(jpeg())

    def change_first_frame():
        if len(actor.prompts) == 1:
            screen._on_jpeg(jpeg("black"))

    actor = Actor(["DO: key a", "DO: replan", "DO: key x", "DO: done"], change_first_frame)
    agent, written = asyncio.run(run_agent(tmp_path, screen, actor))

    assert written == []
    assert len(actor.prompts) == 2 and len(agent.planner.prompts) == 1
    assert agent.steps[-1]["action"] == "fail"


def test_identical_pixels_after_mirror_reconnect_do_not_trigger_recovery(tmp_path):
    screen = Screen()
    original = jpeg()
    screen._on_jpeg(original)

    def reconnect():
        if len(actor.prompts) == 1:
            screen._on_ready()
            screen._on_jpeg(original)

    actor = Actor(["DO: key a", "DO: key x", "DO: done"], reconnect)
    agent, written = asyncio.run(run_agent(tmp_path, screen, actor))

    assert written == []
    assert len(actor.prompts) == 1 and len(agent.planner.prompts) == 1
    assert agent.steps[-1]["action"] == "fail"


def test_change_between_actions_stops_batch_without_reissuing_the_first_key(tmp_path):
    async def scenario():
        screen = Screen()
        screen._on_jpeg(jpeg())

        class ChangeAfterKey(Link):
            def write(self, data):
                super().write(data)
                if len(self.written) == 1:
                    screen._on_jpeg(jpeg("black"))

        actor = Actor(["DO: key a\nDO: key b", "DO: key a", "DO: done"])
        agent, written = await run_agent(tmp_path, screen, actor, ChangeAfterKey())

        assert written == [b"1 key a\n"]
        assert len(actor.prompts) == 1 and len(agent.planner.prompts) == 1
        assert agent.steps[-1]["action"] == "fail"

    asyncio.run(scenario())


def test_change_during_type_ack_keeps_uncertain_input_and_stops(tmp_path):
    async def scenario():
        screen = Screen()
        screen._on_jpeg(jpeg())

        class ChangeAfterType(Link):
            def write(self, data):
                super().write(data)
                if data.endswith(b" type hello\n"):
                    screen._on_jpeg(jpeg("black"))

        actor = Actor(["DO: type hello", "DO: key x", "DO: done"])
        agent, written = await run_agent(tmp_path, screen, actor, ChangeAfterType())

        assert written == [b"1 type hello\n"]
        assert len(actor.prompts) == 1 and len(agent.planner.prompts) == 1
        assert agent.steps[-2]["outcome"] == "unknown"
        assert agent.steps[-1]["action"] == "fail"

    asyncio.run(scenario())


def test_change_during_tap_positioning_stops_at_the_final_guard(tmp_path, monkeypatch):
    screen = Screen()
    screen._on_jpeg(jpeg())

    async def point(self, x, y):
        screen._on_jpeg(jpeg("black"))
        self._pointer_confirmed = True
        return x, y

    monkeypatch.setattr(Phone, "point_verified", point)
    actor = Actor(["DO: tap 150 260", "DO: key x", "DO: done"])
    agent, written = asyncio.run(run_agent(tmp_path, screen, actor))

    assert written == []
    assert len(actor.prompts) == 1 and len(agent.planner.prompts) == 1
    assert agent.steps[-1]["action"] == "fail"
