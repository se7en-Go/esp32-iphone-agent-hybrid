"""Force the stale-save and check/write races without timing-dependent sleeps."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from hidagent.device import Device, DeviceError
from hidagent.guard import Guard


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path))
    from hidagent import app as module

    return module


class RecordingLink:
    def __init__(self):
        self.writes = []
        self.reply = b""

    def is_open(self):
        return True

    def write(self, data):
        self.writes.append(data.decode().strip())
        self.reply = data.split(b" ", 1)[0] + b" ok\n"

    async def readline(self):
        return self.reply

    def close(self):
        pass


def send(device, command):
    try:
        asyncio.run(device.send(command))
    except DeviceError as exc:
        return str(exc)
    return "ok"


@pytest.mark.parametrize("reserved", [False, True])
def test_trip_cannot_be_overwritten_by_inflight_send(tmp_path, monkeypatch, app_module, reserved):
    module = app_module
    now = datetime(2023, 11, 14, 12).timestamp()
    path = tmp_path / "guard.json"
    guard = Guard(clock=lambda: now, path=path)
    if reserved:
        assert guard.prepare_send()["allowed"]
    monkeypatch.setattr(module, "guard", guard)
    device, link = Device(), RecordingLink()
    device.attach(link)
    device.gate = module._hid_gate
    snapshot_taken = threading.Event()
    observer_reached = threading.Event()
    trip_done = threading.Event()
    role = threading.local()
    original_lock = guard._lock

    class OrderedLock:
        """Keep the real lock; schedule the observer before the next write lock.

        Old observe never acquires it: it can persist true while the false snapshot
        is paused. Fixed observe blocks on it, then trips after the sender unlocks.
        """

        def __enter__(self):
            if getattr(role, "observer", False):
                observer_reached.set()
            original_lock.acquire()
            role.depth = getattr(role, "depth", 0) + 1
            return self

        def __exit__(self, *_args):
            role.depth -= 1
            original_lock.release()
            if not role.depth and not getattr(role, "observer", False) and snapshot_taken.is_set():
                assert trip_done.wait(5), "observer did not finish after sender unlocked"

    monkeypatch.setattr(guard, "_lock", OrderedLock())
    write_text = Path.write_text

    def pause_snapshot(target, payload, *args, **kwargs):
        if target == path.with_suffix(".json.tmp") and not json.loads(payload)["tripped"]:
            snapshot_taken.set()
            assert observer_reached.wait(5), "observer never reached the save/lock"
        return write_text(target, payload, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", pause_snapshot)

    def trip():
        role.observer = True
        try:
            return guard.observe("操作频繁")
        finally:
            # On the old implementation this releases the paused stale snapshot.
            observer_reached.set()
            trip_done.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        sending = workers.submit(send, device, "key enter")
        assert snapshot_taken.wait(5), "send did not reach persistence"
        observing = workers.submit(trip)
        assert observing.result(timeout=5)["stop"]
        outcome = sending.result(timeout=5)

    persisted = json.loads(path.read_text(encoding="utf-8"))["tripped"]
    reloaded = Guard(path=path).tripped
    assert guard.tripped
    assert (persisted, reloaded, link.writes) == (True, True, []), (
        f"persisted.tripped={persisted}, reloaded.tripped={reloaded}, serial={link.writes}"
    )
    assert "熔断" in outcome


@pytest.mark.parametrize("command", [
    "key enter", "key a", "type hello", "click", "move 1 1", "press", "scroll 1", "homescreen",
])
def test_trip_between_gate_and_serial_write_blocks_hid(tmp_path, monkeypatch, app_module, command):
    module = app_module
    guard = Guard(clock=lambda: datetime(2023, 11, 14, 12).timestamp(), path=tmp_path / "guard.json")
    monkeypatch.setattr(module, "guard", guard)
    device, link = Device(), RecordingLink()
    device.attach(link)
    checked, resume = threading.Event(), threading.Event()

    def paused_gate(command):
        fence = module._hid_gate(command)
        checked.set()
        assert resume.wait(5)
        return fence

    device.gate = paused_gate
    with ThreadPoolExecutor(max_workers=1) as workers:
        sending = workers.submit(send, device, command)
        try:
            assert checked.wait(5)
            assert guard.observe("验证码")["stop"]
        finally:
            resume.set()
        outcome = sending.result(timeout=5)
    assert link.writes == []
    assert "熔断" in outcome


def test_tripped_guard_still_allows_emergency_release_home(tmp_path, monkeypatch, app_module):
    module = app_module
    guard = Guard(path=tmp_path / "guard.json")
    monkeypatch.setattr(module, "guard", guard)
    device, link = Device(), RecordingLink()
    device.attach(link)
    device.gate = module._hid_gate
    assert guard.observe("风控")["stop"]

    async def stop():
        await device.send("release")
        await device.send("home")

    asyncio.run(stop())
    assert link.writes == ["1 release", "2 home"]
    assert Guard(path=guard._path).tripped
