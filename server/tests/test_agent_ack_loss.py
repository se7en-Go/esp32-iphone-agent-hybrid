"""A lost click ACK must not become a second click after automatic recovery."""

import asyncio
import time
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
def test_click_ack_loss_is_retained_across_reconnect_and_replan(tmp_path, monkeypatch, command, retry=None):
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
        actor = Model([f"DO: {command}", f"DO: {retry or command}", "DO: done inspect outcome"])
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
            ocr.TextBox("发送", (.7, .7, .9, .8), (.8, .75), .99),
            ocr.TextBox("设置", (.1, .3, .3, .4), (.2, .35), .99)]))
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


@pytest.mark.parametrize("command,written_suffix", [
    ("key enter", b" key enter\n"),
    ("type hello", b" type hello\n"),
    ("path 300 500 400 550", b" press\n"),
])
def test_lost_input_ack_is_not_replayed(tmp_path, monkeypatch, command, written_suffix):
    async def scenario():
        class LostAck(Link):
            async def readline(self):
                if self.written[-1].endswith(written_suffix):
                    raise OSError("the line was written, but its ACK was lost")
                return await super().readline()

        screen, device = Screen(), Device()
        original, replacement = LostAck(), Link()
        screen._on_jpeg(jpeg())
        device.attach(original)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        phone.span = {"w": 500, "h": 1080}

        async def positioned(x, y, **kwargs):
            await phone.point(x, y)
            phone._pointer_confirmed = True
            return x, y

        # This test exercises an ACK lost after press, past the separately tested landing gate.
        monkeypatch.setattr(phone, "point_verified", positioned)
        actor = Model([f"DO: {command}", f"DO: {command}", "DO: done"])
        planner = Model(["1. Send the text", "1. Inspect after reconnect"])
        agent = Agent(phone, screen, actor, planner, Trace(None))

        async def reconnect(_timeout):
            device.attach(replacement)
            screen._on_ready()
            screen._on_jpeg(jpeg())
            return True

        async def settled():
            pass

        monkeypatch.setattr(device, "wait_connected", reconnect)
        monkeypatch.setattr(screen, "settled", settled)
        try:
            await agent._run()
            writes = original.written + replacement.written
            assert sum(line.endswith(written_suffix) for line in writes) == 1
            attempts = [step for step in agent.steps if step["action"] == command.split()[0]]
            assert len(attempts) == 1
            assert attempts[0]["outcome"] == "unknown"
            assert attempts[0]["changed"] is None and attempts[0]["fresh"] is False
            actions = [step["action"] for step in agent.steps]
            assert actions.index(attempts[0]["action"]) < actions.index("hiccup")
            assert "outcome is unknown" in actor.prompts[-1]
            assert actions[-1] == "done"
        finally:
            device.close()

    asyncio.run(scenario())


def test_precise_flag_does_not_bypass_an_uncertain_tap(tmp_path, monkeypatch):
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
        actor = Model(["DO: tap 800 750", "DO: tap 800 750 precise", "DO: done"])
        planner = Model(["1. Tap", "1. Inspect"])
        agent = Agent(phone, screen, actor, planner, Trace(None))

        async def reconnect(_timeout):
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
        try:
            await agent._run()
            writes = original.written + replacement.written
            assert sum(line.endswith(b" click\n") for line in writes) == 1
            assert "outcome is unknown" in actor.prompts[-1]
        finally:
            device.close()

    asyncio.run(scenario())


def test_lost_enter_ack_counts_once_through_the_production_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path))
    from hidagent import app as app_module
    from hidagent.guard import Guard

    async def scenario():
        class LostAck(Link):
            async def readline(self):
                if self.written[-1].endswith(b" key enter\n"):
                    raise OSError("enter was written, but its ACK was lost")
                return await super().readline()

        local_noon = time.mktime((2024, 1, 15, 12, 0, 0, 0, 0, -1))
        guard = Guard(clock=lambda: local_noon)
        screen, device = Screen(), Device()
        original, replacement = LostAck(), Link()
        screen._on_jpeg(jpeg())
        device.attach(original)
        device.gate = app_module._hid_gate
        monkeypatch.setattr(app_module, "guard", guard)
        phone = Phone(device, tmp_path / "calibration.json", screen)
        actor = Model(["DO: key enter", "DO: key enter", "DO: done"])
        planner = Model(["1. Send", "1. Inspect"])
        agent = Agent(phone, screen, actor, planner, Trace(None))

        async def reconnect(_timeout):
            device.attach(replacement)
            replacement_gate = replacement
            replacement_gate.written = replacement.written
            screen._on_ready()
            screen._on_jpeg(jpeg())
            return True

        async def settled():
            pass

        monkeypatch.setattr(device, "wait_connected", reconnect)
        monkeypatch.setattr(screen, "settled", settled)
        try:
            await agent._run()
            writes = original.written + replacement.written
            assert sum(line.endswith(b" key enter\n") for line in writes) == 1
            assert len(guard.sends) == 1
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("retry", [
    'tap_text {"text":"设置","exact":false}',
    'tap_text {"text":"设 置","exact":false,"region":null}',
    'tap_text {"text":"设置","nth":1}',
    'tap_text {"text":"设置","region":[0,0,1,1],"exact":true}',
])
def test_equivalent_ocr_selectors_do_not_replay_after_lost_click_ack(tmp_path, monkeypatch, retry):
    test_click_ack_loss_is_retained_across_reconnect_and_replan(
        tmp_path, monkeypatch, "tap_text 设置", retry=retry)
