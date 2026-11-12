"""A lost click ACK must not become a second click after automatic recovery."""

import asyncio
from types import SimpleNamespace

import pytest
from test_agent_context import Model
from test_control_input_context import Link
from test_controls import jpeg

from hidagent import ocr
from hidagent.agent import Agent
from hidagent.debug import Trace
from hidagent.device import Device
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.mark.parametrize("command", ["tap 800 750", 'tap_text {"text":"发送","region":[0,0.6,1,0.9]}'])
def test_click_ack_loss_is_retained_across_reconnect_and_replan(tmp_path, monkeypatch, command):
    async def scenario():
        class LostAck(Link):
            async def readline(self):
                if self.written[-1].endswith(b" click\n"):
                    raise OSError("click was written, but its ACK was lost")
                return await super().readline()

        screen, device = Screen(), Device()
        original, replacement = LostAck(), Link()
        screen._on_jpeg(jpeg())
        device.attach(original)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        actor = Model([f"DO: {command}", f"DO: {command}", "DO: done inspect outcome"])
        planner = Model(["1. Tap the observed target", "1. Inspect after reconnect"])
        agent = Agent(phone, screen, actor, planner, Trace(None))
        recoveries = []

        async def reconnect(_timeout):
            recoveries.append(True)
            device.attach(replacement)
            screen._on_ready()
            screen._on_jpeg(jpeg())
            return True

        async def positioned(x, y):
            phone._pointer_confirmed = True
            return x, y

        async def effect(*_args):
            phone._effect_frame = phone._effect_snapshot = None
            return False, False

        async def settled():
            pass

        monkeypatch.setattr(device, "wait_connected", reconnect)
        monkeypatch.setattr(phone, "point_verified", positioned)
        monkeypatch.setattr(phone, "_tap_effect", effect)
        monkeypatch.setattr(screen, "settled", settled)
        ocr.set_backend(SimpleNamespace(read_text=lambda _frame: [
            ocr.TextBox("发送", (.7, .7, .9, .8), (.8, .75), .99)]))
        try:
            await agent._run()
            writes = original.written + replacement.written
            assert sum(line.endswith(b" click\n") for line in writes) == 1
            assert recoveries == [True]
            attempts = [step for step in agent.steps if step["action"] in {"tap", "tap_text"}]
            assert len(attempts) == 1
            assert attempts[0]["changed"] is None and attempts[0]["fresh"] is False
            assert attempts[0]["effect_verified"] is False
            assert attempts[0]["outcome"] == "unknown"
            actions = [step["action"] for step in agent.steps]
            assert actions.index(attempts[0]["action"]) < actions.index("hiccup")
            assert "outcome is unknown" in actor.prompts[-1]
            assert actions[-1] == "done"
        finally:
            ocr.set_backend(None)
            device.close()

    asyncio.run(scenario())
