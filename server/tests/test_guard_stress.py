"""Threaded callers and asyncio senders share one persistent circuit breaker."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event, Lock

from hidagent.device import Device, DeviceError
from hidagent.guard import Guard


def test_concurrent_sends_consume_trip_and_save_stay_fail_closed(tmp_path, monkeypatch):
    # Importing app creates its data directory; standalone runs must also stay in tmp_path.
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path))
    from hidagent import app as module

    path = tmp_path / "guard.json"
    noon = datetime(2023, 11, 14, 12, 0).timestamp()
    now = [noon]
    guard = Guard(clock=lambda: now[0], path=path)
    monkeypatch.setattr(module, "guard", guard)
    records_lock = Lock()
    writes = []
    failures = []
    decisions = []
    snapshots = []
    first_after_window = Event()
    tripped = Event()
    real_replace = os.replace

    def checked_replace(source, target):
        real_replace(source, target)
        if target == path:
            # Read and close before the saving caller releases the guard lock. This checks
            # every published file without keeping a Windows handle open across rename.
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            with records_lock:
                snapshots.append(snapshot)

    monkeypatch.setattr("hidagent.guard.os.replace", checked_replace)

    class Link:
        def __init__(self):
            self.replies = asyncio.Queue()
            self.closed = False

        def is_open(self):
            return not self.closed

        def close(self):
            self.closed = True

        def write(self, data):
            with records_lock:
                writes.append((guard.tripped, tripped.is_set(), data))
            if now[0] >= noon + 3601:
                first_after_window.set()
            request_id = data.split(b" ", 1)[0]
            self.replies.put_nowait(request_id + b" ok\n")

        async def readline(self):
            return await self.replies.get()

    def new_device():
        device = Device()
        device.attach(Link())
        device.gate = module._hid_gate
        return device

    async def send(device, command):
        try:
            await device.send(command)
        except DeviceError as exc:
            with records_lock:
                failures.append(str(exc))

    async def prime():
        device = new_device()
        try:
            await device.send("key enter")
        finally:
            device.close()

    asyncio.run(prime())
    assert len(writes) == 1

    senders = 4
    quota_rounds = 22
    rounds = quota_rounds + 8
    # Four event-loop threads, direct consume/save/observe threads, and this test thread.
    boundary = Barrier(senders + 4, timeout=10)

    def sender():
        async def scenario():
            device = new_device()
            try:
                for _round in range(rounds):
                    boundary.wait()
                    await asyncio.gather(
                        *(send(device, command) for command in
                          ("key enter", "key enter", "key a", "key enter"))
                    )
                    boundary.wait()
            finally:
                device.close()

        try:
            asyncio.run(scenario())
        except BaseException:
            boundary.abort()
            raise

    def background(kind):
        try:
            for round_number in range(rounds):
                boundary.wait()
                if kind == "consume":
                    for _ in range(4):
                        decision = guard.consume_reservation()
                        with records_lock:
                            decisions.append(decision)
                elif kind == "save":
                    for _ in range(4):
                        guard._save()
                elif round_number == quota_rounds:
                    assert first_after_window.wait(5), "senders did not reach the new hourly window"
                    assert guard.observe("操作频繁")["stop"]
                    tripped.set()
                else:
                    guard.observe("操作频繁" if tripped.is_set() else "普通文字")
                boundary.wait()
        except BaseException:
            boundary.abort()
            raise

    with ThreadPoolExecutor(max_workers=senders + 3) as workers:
        futures = [workers.submit(sender) for _ in range(senders)]
        futures += [workers.submit(background, kind) for kind in ("consume", "save", "observe")]
        try:
            for round_number in range(rounds):
                # The barriers ensure no worker reads the clock during this update.
                now[0] = noon + (round_number + 1) * 9 if round_number < quota_rounds else noon + 3601
                boundary.wait()
                boundary.wait()
                if round_number == quota_rounds - 1:
                    assert len(guard.sends) == 20
                    assert any("这一小时已经发送" in reason for reason in failures)
                    assert any("这一小时已经发送" in decision["reason"] for decision in decisions)
        except BaseException:
            boundary.abort()
            raise
        for future in futures:
            future.result(timeout=5)

    assert tripped.is_set() and first_after_window.is_set()
    assert guard.tripped and Guard(path=path).tripped
    assert json.loads(path.read_text(encoding="utf-8"))["tripped"] is True
    assert snapshots and any(snapshot["tripped"] for snapshot in snapshots)
    assert all(len(snapshot["sends"]) <= 20 for snapshot in snapshots)
    first_trip = next(index for index, snapshot in enumerate(snapshots) if snapshot["tripped"])
    assert all(snapshot["tripped"] for snapshot in snapshots[first_trip:])
    assert all(not memory_trip and not returned_trip for memory_trip, returned_trip, _ in writes)
    assert any("熔断" in reason for reason in failures)
    assert any(decision["stop"] for decision in decisions)
