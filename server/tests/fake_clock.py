"""Explicit wall/monotonic time and scheduled frames, independent of host timers."""

import asyncio
import heapq
import itertools
import time


class ManualClock:
    def __init__(self, *, wall=1_800_000_000.0, monotonic=100.0):
        self.wall_origin = wall
        self.mono_origin = monotonic
        self.elapsed = 0.0
        self._scheduled = []
        self._sequence = itertools.count()
        self._yield = asyncio.sleep

    def time(self):
        return self.wall_origin + self.elapsed

    def monotonic(self):
        return self.mono_origin + self.elapsed

    def call_later(self, delay, callback, *args):
        heapq.heappush(self._scheduled, (self.elapsed + delay, next(self._sequence), callback, args))

    def advance(self, seconds):
        target = self.elapsed + seconds
        while self._scheduled and self._scheduled[0][0] <= target:
            self.elapsed, _, callback, args = heapq.heappop(self._scheduled)
            callback(*args)
        self.elapsed = target

    async def sleep(self, seconds, result=None):
        self.advance(max(0, seconds))
        await self._yield(0)
        return result

    def install(self, monkeypatch):
        monkeypatch.setattr(time, "time", self.time)
        monkeypatch.setattr(time, "monotonic", self.monotonic)
        monkeypatch.setattr(time, "perf_counter", self.monotonic)
        monkeypatch.setattr(asyncio, "sleep", self.sleep)
