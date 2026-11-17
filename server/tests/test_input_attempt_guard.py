"""An inherited action session gate covers every actual HID write, including probes."""

import asyncio

import pytest
from test_control_input_context import Link

from hidagent.device import HID_COMMANDS, Device, input_attempt


@pytest.mark.parametrize("command", sorted(HID_COMMANDS - {"release"}))
def test_session_gate_runs_in_child_exchange_before_any_hid_write(command):
    async def scenario():
        device, link = Device(), Link()
        device.attach(link)

        def reject():
            raise RuntimeError("session changed")

        try:
            with input_attempt(before_write=reject) as attempt:
                with pytest.raises(RuntimeError, match="session changed"):
                    await device.send(command)
                assert attempt.written is False
                assert attempt.started is False
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


def test_invalid_session_allows_release_cleanup_and_does_not_leak_to_the_next_action():
    async def scenario():
        device, link = Device(), Link()
        device.attach(link)

        def reject():
            raise RuntimeError("session changed")

        try:
            with input_attempt(before_write=reject) as attempt:
                await device.send("release")
                assert attempt.written is False
                assert attempt.started is False
            await device.send("key x")
            assert link.written == [b"1 release\n", b"2 key x\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", sorted(HID_COMMANDS - {"release"}))
def test_every_noncleanup_hid_starts_only_at_the_actual_write(command):
    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        states = []
        write = link.write
        try:
            with input_attempt(before_write=lambda: states.append(attempt.started)) as attempt:
                def record(data):
                    assert attempt.started
                    write(data)
                link.write = record
                await device.send(command)
                assert attempt.started and states == [False]
                assert attempt.written is (command in {"click", "key", "type", "press"})
        finally:
            device.close()
    asyncio.run(scenario())


def test_gate_commit_failure_does_not_mark_started():
    from contextlib import contextmanager

    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        @contextmanager
        def gate(_command):
            def reject():
                raise RuntimeError("commit refused")
            yield reject
        device.gate = gate
        try:
            with input_attempt() as attempt:
                with pytest.raises(RuntimeError, match="commit refused"):
                    await device.send("move 1 0")
                assert not attempt.started and not attempt.written and not link.written
        finally:
            device.close()
    asyncio.run(scenario())


def test_unknown_press_ack_is_started_and_released_without_retry(monkeypatch):
    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        monkeypatch.setattr(device, "_timeout", lambda _command: .01)
        write = link.write
        try:
            with input_attempt() as attempt:
                def no_press_ack(data):
                    if b" press" in data:
                        assert attempt.started and attempt.written
                        link.written.append(data)
                    else:
                        write(data)
                link.write = no_press_ack
                with pytest.raises(RuntimeError):
                    await device.send("press")
                assert attempt.started and attempt.written
                assert [line.split(b" ", 1)[1].strip() for line in link.written] == [b"press", b"release"]
        finally:
            device.close()
    asyncio.run(scenario())
