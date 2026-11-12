"""Agent recovery must retain actual writes and serialize only public evidence."""

import asyncio
from contextlib import nullcontext

import pytest
from test_agent_context import Model
from test_control_input_context import Link
from test_controls import jpeg

from hidagent.agent import Agent
from hidagent.debug import Trace
from hidagent.device import Device, DeviceError
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.mark.parametrize("command,suffix,reply", [
    ("type hello", b" type hello\n", b"err partial type"),
    ("key enter", b" key enter\n", b"err partial key"),
    ("type hello", b" type hello\n", b"err unknown execution status"),
])
def test_firmware_partial_or_unknown_input_is_not_replayed(tmp_path, monkeypatch, command, suffix, reply):
    async def scenario():
        class FirmwareFailure(Link):
            def write(self, data):
                self.written.append(data)
                response = reply if data.endswith(suffix) else b"ok"
                self.replies.put_nowait(data.split(b" ", 1)[0] + b" " + response + b"\n")

        screen, device, link = Screen(), Device(), FirmwareFailure()
        screen._on_jpeg(jpeg())
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        actor = Model([f"DO: {command}", f"DO: {command}", "DO: done inspect outcome"])
        agent = Agent(phone, screen, actor, Model(["1. Input", "1. Inspect"]), Trace(None))

        async def settled():
            pass

        monkeypatch.setattr(screen, "settled", settled)
        try:
            await agent._run()
            assert sum(data.endswith(suffix) for data in link.written) == 1
            attempts = [step for step in agent.steps if step["action"] in {"type", "key"}]
            assert len(attempts) == 1
            assert attempts[0]["outcome"] == "unknown"
            assert attempts[0]["changed"] is None
            assert "outcome is unknown" in actor.prompts[-1]
            assert agent.steps[-1]["action"] == "done"
        finally:
            device.close()

    asyncio.run(scenario())


def test_rejected_input_before_serial_write_can_retry_after_recovery(tmp_path, monkeypatch):
    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        gates = []

        def gate(_command):
            gates.append(True)
            if len(gates) == 1:
                raise DeviceError("temporarily blocked before write")
            return nullcontext()

        device.gate = gate
        phone = Phone(device, tmp_path / "calibration.json", screen)
        agent = Agent(phone, screen, Model(["DO: type hello", "DO: type hello", "DO: done"]),
                      Model(["1. Input", "1. Retry after recovery"]), Trace(None))

        async def settled():
            pass

        monkeypatch.setattr(screen, "settled", settled)
        try:
            await agent._run()
            assert sum(data.endswith(b" type hello\n") for data in link.written) == 1
            attempts = [step for step in agent.steps if step["action"] == "type"]
            assert len(attempts) == 1
            assert "outcome" not in attempts[0]
            assert agent.steps[-1]["action"] == "done"
        finally:
            device.close()

    asyncio.run(scenario())


def test_successful_real_phone_typing_records_serializable_history_then_done(tmp_path, monkeypatch):
    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        actor = Model(["DO: type hello", "DO: done typed"])
        trace = Trace(tmp_path / "traces")
        trace.start("Type hello")
        agent = Agent(phone, screen, actor, Model(["1. Type hello"]), trace)

        async def settled():
            pass

        monkeypatch.setattr(screen, "settled", settled)
        try:
            await agent._run()
            assert link.written == [b"1 type hello\n"]
            assert len(actor.prompts) == 2
            assert agent.steps[-1]["action"] == "done"
            assert agent.steps[-1]["answer"] == "typed"
            typed = next(step for step in agent.steps if step["action"] == "type")
            assert all(not key.startswith("_") for key in typed)
            assert '"action": "type"' in actor.prompts[-1]
        finally:
            device.close()

    asyncio.run(scenario())


def test_completed_chunk_before_later_prewrite_failure_is_not_replayed(tmp_path, monkeypatch):
    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        writes_authorized = []

        def gate(command):
            writes_authorized.append(command)
            if len(writes_authorized) == 2:
                raise DeviceError("second chunk rejected before write")
            return nullcontext()

        device.gate = gate
        phone = Phone(device, tmp_path / "calibration.json", screen)
        text = "a" * 600
        actor = Model([f"DO: type {text}", f"DO: type {text}", "DO: done inspect first chunk"])
        agent = Agent(phone, screen, actor, Model(["1. Input", "1. Inspect"]), Trace(None))

        async def settled():
            pass

        monkeypatch.setattr(screen, "settled", settled)
        try:
            await agent._run()
            assert len(link.written) == 1
            assert link.written[0] == b"1 type " + b"a" * 499 + b"\n"
            attempts = [step for step in agent.steps if step["action"] == "type"]
            assert len(attempts) == 1
            assert attempts[0]["outcome"] == "unknown"
            assert "outcome is unknown" in actor.prompts[-1]
            assert agent.steps[-1]["action"] == "done"
        finally:
            device.close()

    asyncio.run(scenario())
