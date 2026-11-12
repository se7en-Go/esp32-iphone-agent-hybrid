"""Small screenshot deltas with optional, time-bounded OCR feedback."""

import asyncio
import hashlib
import io
import math
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from PIL import Image, ImageChops

from . import ocr

OCR_BUDGET_SECONDS = 0.25
MAX_REGIONS = 12


def _pixels(before: bytes, after: bytes) -> dict:
    with Image.open(io.BytesIO(before)) as first, Image.open(io.BytesIO(after)) as second:
        if first.size != second.size:
            return {"changed_regions": [[0.0, 0.0, 1.0, 1.0]], "changed_fraction": 1.0, "size_changed": True}
        width, height = first.size
        scale = min(1, 180 / width, 390 / height)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        a = first.convert("RGB").resize(size, Image.Resampling.BOX)
        b = second.convert("RGB").resize(size, Image.Resampling.BOX)
    red, green, blue = ImageChops.difference(a, b).split()
    mask = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(lambda value: 255 if value > 24 else 0)
    changed = mask.histogram()[255]
    width, height = size
    cw, ch = max(1, (width + 5) // 6), max(1, (height + 5) // 6)
    # A cell needs about two changed samples; this rejects isolated compression noise.
    cells = mask.resize((cw, ch), Image.Resampling.BOX).tobytes()
    hot = {(i % cw, i // cw) for i, value in enumerate(cells) if value >= 14}
    regions = []
    while hot:
        stack = [hot.pop()]
        left = right = stack[0][0]
        top = bottom = stack[0][1]
        while stack:
            x, y = stack.pop()
            left, top, right, bottom = min(left, x), min(top, y), max(right, x), max(bottom, y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbor = (x + dx, y + dy)
                    if neighbor in hot:
                        hot.remove(neighbor)
                        stack.append(neighbor)
        regions.append([left / cw, top / ch, (right + 1) / cw, (bottom + 1) / ch])
    regions.sort(key=lambda box: (-(box[2] - box[0]) * (box[3] - box[1]), box[1], box[0]))
    if len(regions) > MAX_REGIONS:
        rest = regions[MAX_REGIONS - 1:]
        regions = regions[:MAX_REGIONS - 1] + [[min(box[0] for box in rest), min(box[1] for box in rest),
                                               max(box[2] for box in rest), max(box[3] for box in rest)]]
    return {"changed_regions": [[math.floor(box[0] * 10000) / 10000, math.floor(box[1] * 10000) / 10000,
                                  math.ceil(box[2] * 10000) / 10000, math.ceil(box[3] * 10000) / 10000]
                                 for box in regions],
            "changed_fraction": round(changed / (width * height), 5), "size_changed": False}


def _text_changes(before, after) -> dict:
    def difference(source, other):
        remaining = Counter(ocr.normalize_text(box.text) for box in other)
        result = []
        for box in source:
            key = ocr.normalize_text(box.text)
            if not key:
                continue
            if remaining[key]:
                remaining[key] -= 1
            else:
                result.append(box.text)
        return result[:30]

    return {"texts_appeared": difference(after, before), "texts_disappeared": difference(before, after),
            "text_status": "available"}


@dataclass
class _Job:
    keys: tuple[bytes, bytes]
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None


class _TextWorker:
    """One daemon job, no backlog; cancellation never waits for native inference."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active: _Job | None = None
        self._cache: OrderedDict = OrderedDict()

    def submit(self, before: bytes, after: bytes) -> _Job | None:
        keys = hashlib.sha256(before).digest(), hashlib.sha256(after).digest()
        with self._lock:
            if all(key in self._cache for key in keys):
                job = _Job(keys, result=_text_changes(*(self._cache[key] for key in keys)))
                job.done.set()
                return job
            if self._active:
                return self._active if self._active.keys == keys else None
            job = self._active = _Job(keys)
        threading.Thread(target=self._run, args=(job, before, after), name="screen-diff-ocr", daemon=True).start()
        return job

    def _read(self, key, frame):
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        boxes = ocr.read_text(frame)
        with self._lock:
            self._cache[key] = boxes
            while len(self._cache) > 2:
                self._cache.popitem(last=False)
        return boxes

    def _run(self, job, before, after):
        try:
            job.result = _text_changes(self._read(job.keys[0], before), self._read(job.keys[1], after))
        except Exception:
            # Optional OCR must not turn an acknowledged action into a failed request.
            job.result = {"texts_appeared": None, "texts_disappeared": None, "text_status": "error"}
        finally:
            with self._lock:
                self._active = None
                job.done.set()


_worker = _TextWorker()


async def summarize(before: bytes | None, after: bytes | None, *, fresh: bool = True,
                    ocr_enabled: bool = True) -> dict:
    """Compare returned frames, not intent. Unknown text changes are null, never an empty list."""
    result = {"status": "ok", "changed_regions": [], "changed_fraction": None, "size_changed": None,
              "texts_appeared": None, "texts_disappeared": None, "text_status": "skipped"}
    if not fresh:
        return {**result, "status": "stale"}
    if not before or not after:
        return {**result, "status": "missing"}
    try:
        result.update(_pixels(before, after))
    except (OSError, ValueError, Image.DecompressionBombError):
        return {**result, "status": "invalid"}
    if not ocr_enabled:
        return {**result, "text_status": "disabled"}
    if before == after:
        return {**result, "texts_appeared": [], "texts_disappeared": [], "text_status": "unchanged"}
    if not ocr.available():
        return {**result, "text_status": "unavailable"}
    job = _worker.submit(before, after)
    if job is None:
        return {**result, "text_status": "busy"}
    deadline = time.monotonic() + OCR_BUDGET_SECONDS
    while not job.done.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "text_status": "timeout"}
        await asyncio.sleep(min(0.01, remaining))
    return {**result, **job.result}
