"""Known firmware ACKs and later field verification are separate facts."""

import asyncio

import pytest
from input_scene import input_frame
from test_control_input_context import Link

from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.device import Device
from hidagent.phone import TYPE_LIMIT, Phone
from hidagent.screen import Screen


@pytest.fixture
def rig(tmp_path, monkeypatch):
    screen, device = Screen(), Device()
    screen._on_jpeg(input_frame())

    class ChangingAck(Link):
        fail_after = 1
        read_count = 0
        unknown = False
        unknown_after = None
        firmware_error = False

        async def readline(self):
            if self.unknown or self.read_count + 1 == self.unknown_after:
                raise OSError("simulated lost ACK")
            reply = await super().readline()
            self.read_count += 1
            if self.firmware_error:
                return reply.replace(b" ok", b" error")
            if self.read_count == self.fail_after:
                screen._on_jpeg(input_frame(typed=True, replaced=True))
            return reply

    link = ChangingAck()
    device.attach(link)
    phone = Phone(device, tmp_path / 'calibration.json', screen)
    controls = Controls(phone, screen)

    async def response(_before, completed, _began, _ready, acknowledged, error, extra, *_args):
        return {"command_acknowledged": acknowledged, "error": error,
                "completed_at": completed, "effect_verified": False, **extra}

    monkeypatch.setattr(controls, '_respond', response)
    yield controls, phone, device, link, screen
    device.close()


@pytest.mark.parametrize("action,text", [("compose_chinese", "你好"), ("type", "hello")])
def test_single_type_ack_remains_known_when_accept_validation_fails(rig, action, text):
    controls, _, _, link, _ = rig
    result = asyncio.run(controls.perform(ControlAction(
        action=action, text=text, input_mode="pinyin", screen_id=controls.observation()["screen_id"])))
    assert len(link.written) == link.read_count == 1
    assert result["command_acknowledged"] is True
    assert "changed" in result["error"] and result["requires_verification"] is True
    assert result["effect_verified"] is False and result["input_verified"] is False
    assert result["typing_progress"] == {"chunks_acknowledged": 1, "chunks_total": 1,
                                         "characters_acknowledged": 5, "all_chunks_acknowledged": True}


@pytest.mark.parametrize("failed_chunk", [1, 2])
@pytest.mark.parametrize("batch", [False, True])
def test_acknowledged_chunks_are_reported_and_remaining_input_is_not_sent(rig, failed_chunk, batch):
    controls, _, _, link, _ = rig
    link.fail_after = failed_chunk
    text = "x" * (TYPE_LIMIT * 2 + 1)
    request = ControlAction(action="type", text=text, screen_id=controls.observation()["screen_id"])
    call = (controls.perform_batch(request.screen_id, [request, ControlAction(
                action="point", x=.5, y=.5, screen_id=request.screen_id)])
            if batch else controls.perform(request))
    result = asyncio.run(call)
    assert len(link.written) == link.read_count == failed_chunk
    assert result["command_acknowledged"] is True and result["error"]
    assert result["typing_progress"] == {"chunks_acknowledged": failed_chunk, "chunks_total": 3,
                                         "characters_acknowledged": TYPE_LIMIT * failed_chunk,
                                         "all_chunks_acknowledged": False}
    assert result["requires_verification"] and not result["input_verified"]
    if batch:
        assert result["completed"] == 0 and result["stopped_before_step"] == 2
        assert result["steps"][0]["ok"] is False and result["steps"][0]["command_acknowledged"] is True


@pytest.mark.parametrize("kind", ["unknown", "firmware_error"])
def test_unknown_or_negative_ack_never_becomes_known_success(rig, kind):
    controls, _, _, link, _ = rig
    setattr(link, kind, True)
    result = asyncio.run(controls.perform(ControlAction(
        action="type", text="hello", screen_id=controls.observation()["screen_id"])))
    assert result["command_acknowledged"] is False and result["error"]
    assert "typing_progress" not in result and "input_verified" not in result
    assert len(link.written) == 1


def test_a_known_first_chunk_cannot_promote_an_unknown_second_ack(rig):
    controls, _, _, link, _ = rig
    link.fail_after, link.unknown_after = None, 2
    result = asyncio.run(controls.perform(ControlAction(
        action="type", text="x" * (TYPE_LIMIT + 1), screen_id=controls.observation()["screen_id"])))
    assert result["command_acknowledged"] is False and result["error"]
    assert len(link.written) == 2 and link.read_count == 1
    assert "typing_progress" not in result


def test_prewrite_rejection_still_sends_nothing_and_has_no_ack(rig):
    controls, _, device, link, _ = rig

    def rejected(_command):
        raise RuntimeError("simulated prewrite rejection")

    device.gate = rejected
    result = asyncio.run(controls.perform(ControlAction(
        action="type", text="hello", screen_id=controls.observation()["screen_id"])))
    assert not result["command_acknowledged"] and result["error"]
    assert link.written == [] and "typing_progress" not in result


def test_web_agent_stops_after_known_ack_and_does_not_replan_or_repeat(rig, monkeypatch):
    from test_agent_execution import Model

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    _, phone, _, link, screen = rig
    actor = Model(["DO: type hello", "DO: type hello", "DO: done inspected"])
    agent = Agent(phone, screen, actor, Model(["1. Type hello"]), Trace(None))

    async def settled(**_kwargs):
        pass

    monkeypatch.setattr(screen, 'settled', settled)
    asyncio.run(agent._run())
    assert len(link.written) == len(actor.prompts) == 1
    typed = next(step for step in agent.steps if step['action'] == 'type')
    assert typed['outcome'] == 'unknown' and typed['command_acknowledged'] is True
    assert typed['requires_verification'] and not typed['input_verified']
    assert agent.steps[-1]['action'] == 'fail' and agent.steps[-1]['phase'] == 'observation'
