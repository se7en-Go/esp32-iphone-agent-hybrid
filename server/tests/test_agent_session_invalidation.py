"""Execution session changes end a model run regardless of localized error text."""

import asyncio

import pytest
from test_agent_context import Model
from test_agent_observation_recovery import Actor
from test_control_input_context import Link
from test_controls import jpeg

from hidagent.agent import Agent
from hidagent.debug import Trace
from hidagent.device import Device
from hidagent.phone import Phone
from hidagent.screen import Screen


def change_session(screen, device, transition):
    if transition == "generation":
        screen._on_ready()
        screen._on_jpeg(jpeg())
    elif transition == "link":
        device.links += 1
    else:
        device._abort_epoch += 1


@pytest.mark.parametrize("transition", ["generation", "link", "abort"])
@pytest.mark.parametrize("role", ["planner", "actor", "recovery_actor"])
def test_model_wait_keeps_the_device_session_of_its_observation(tmp_path, transition, role):
    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        phone = Phone(device, tmp_path / "cal.json", screen)

        def during():
            if role == "recovery_actor" and len(actor.prompts) == 1:
                screen._on_jpeg(jpeg("black"))
            else:
                change_session(screen, device, transition)

        actor = Actor(["DO: key x", "DO: key y", "DO: done"],
                      during if role != "planner" else None)
        planner = Model(["1. Use current screen"] * 4, during if role == "planner" else None)
        agent = Agent(phone, screen, actor, planner, Trace(None))
        try:
            await agent._run()
            assert link.written == []
            assert len(actor.prompts) == {"planner": 0, "actor": 1, "recovery_actor": 2}[role]
            assert len(planner.prompts) == 1
            assert agent.steps[-1]["action"] == "fail"
            assert agent.steps[-1]["phase"] == "observation"
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["generation", "link", "abort"])
@pytest.mark.parametrize("action", ["key", "tap"])
def test_device_lock_wait_rechecks_original_session_before_any_hid_write(tmp_path, transition, action):
    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        phone = Phone(device, tmp_path / "cal.json", screen)

        class LockedActor(Actor):
            async def chat(self, *args, **kwargs):
                if not self.prompts and action == "key":
                    await device._lock.acquire()
                return await super().chat(*args, **kwargs)

        async def measured_point(x, y, **_kwargs):
            phone._pointer_confirmed = True
            await device._lock.acquire()
            return x, y

        async def tap_effect(*_args, **_kwargs):
            return False, True

        if action == "tap":
            phone.point_verified, phone._tap_effect = measured_point, tap_effect
        actor = LockedActor(["DO: key x" if action == "key" else "DO: tap 150 260", "DO: done"])
        planner = Model(["1. Use current screen"] * 4)
        agent = Agent(phone, screen, actor, planner, Trace(None))
        task = asyncio.create_task(agent._run())
        try:
            async def waiting():
                while not device._lock._waiters:
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            change_session(screen, device, transition)
            device._lock.release()
            await asyncio.wait_for(task, 3)
            assert link.written == []
            assert len(actor.prompts) == 1 and len(planner.prompts) == 1
            assert agent.steps[-1]["action"] == "fail"
            assert agent.steps[-1]["phase"] == "observation"
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["generation", "link", "abort"])
def test_real_pointer_session_failure_never_replans_or_sends_the_next_key(tmp_path, transition):
    async def scenario():
        screen, device, link = Screen(), Device(), Link()
        screen._on_jpeg(jpeg())
        device.attach(link)
        phone = Phone(device, tmp_path / "cal.json", screen)

        async def point(*_args):
            pass

        async def changed_session(*_args, **_kwargs):
            if transition == "generation":
                screen._on_ready()
                screen._on_jpeg(jpeg())
            elif transition == "link":
                device.links += 1
            else:
                device._abort_epoch += 1
            return None

        phone.point, phone._find_pointer = point, changed_session
        actor = Actor(["DO: tap 150 260", "DO: key x", "DO: done"])
        planner = Model(["1. Use current screen"] * 4)
        agent = Agent(phone, screen, actor, planner, Trace(None))
        try:
            await agent._run()
            assert link.written == []
            assert len(actor.prompts) == 1 and len(planner.prompts) == 1
            assert agent.steps[-1]["action"] == "fail"
            assert agent.steps[-1]["phase"] == "observation"
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["generation", "link", "abort"])
def test_successful_ack_with_changed_session_does_not_authorize_next_batch_key(tmp_path, transition):
    async def scenario():
        screen, device = Screen(), Device()
        screen._on_jpeg(jpeg())

        class ChangedAfterWrite(Link):
            def write(self, data):
                super().write(data)
                if len(self.written) == 1:
                    if transition == "generation":
                        screen._on_ready()
                        screen._on_jpeg(jpeg())
                    elif transition == "link":
                        device.links += 1
                    else:
                        device._abort_epoch += 1

        link = ChangedAfterWrite()
        device.attach(link)
        phone = Phone(device, tmp_path / "cal.json", screen)
        actor = Actor(["DO: key a\nDO: key b", "DO: key x", "DO: done"])
        planner = Model(["1. Use current screen"] * 4)
        agent = Agent(phone, screen, actor, planner, Trace(None))
        try:
            await agent._run()
            assert link.written == [b"1 key a\n"]
            assert len(actor.prompts) == 1 and len(planner.prompts) == 1
            assert agent.steps[-1]["action"] == "fail"
            assert agent.steps[-1]["phase"] == "observation"
        finally:
            device.close()

    asyncio.run(scenario())
