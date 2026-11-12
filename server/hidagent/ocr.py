"""Optional, local Chinese/English OCR. Coordinates use the observed frame's 0..1 space."""

import asyncio
import importlib.util
import io
import math
import threading
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from PIL import Image


@dataclass(frozen=True)
class TextBox:
    text: str
    box: tuple[float, float, float, float]  # left, top, right, bottom
    center: tuple[float, float]
    conf: float


class OCRError(RuntimeError):
    """OCR failed without performing a device action."""


class OCRUnavailable(OCRError):
    """The optional OCR runtime cannot be loaded."""


class TextReader(Protocol):
    def read_text(self, frame: bytes) -> list[TextBox]: ...


_backend: TextReader | None = None
_lock = threading.Lock()
_INSTALL = 'OCR 不可用，请安装可选依赖：pip install -e "server[ocr]"'


@dataclass
class _Lookup:
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


_lookup: _Lookup | None = None
_lookup_lock = threading.Lock()


def available() -> bool:
    """Check installation without importing ONNX or loading its models."""
    return _backend is not None or importlib.util.find_spec("rapidocr_onnxruntime") is not None


def normalize_text(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def set_backend(backend: TextReader | None) -> None:
    """Inject a local reader; None restores lazy RapidOCR loading."""
    global _backend
    with _lock:
        _backend = backend
        _read_cached.cache_clear()


def _text_boxes(result, width: int, height: int) -> list[TextBox]:
    boxes = []
    for points, text, score in result or []:
        if not normalize_text(text) or not math.isfinite(float(score)):
            continue
        xs, ys = zip(*points, strict=True)
        if not all(math.isfinite(float(value)) for value in (*xs, *ys)):
            continue
        x0, x1 = (max(0.0, min(1.0, float(x) / max(1, width - 1))) for x in (min(xs), max(xs)))
        y0, y1 = (max(0.0, min(1.0, float(y) / max(1, height - 1))) for y in (min(ys), max(ys)))
        if x1 <= x0 or y1 <= y0:
            continue
        boxes.append(TextBox(text, (x0, y0, x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2),
                             max(0.0, min(1.0, float(score)))))
    return sorted(boxes, key=lambda item: (item.box[1], item.box[0]))


class _RapidOCRBackend:
    def __init__(self):
        try:
            from rapidocr_onnxruntime import RapidOCR

            # Bundled PP-OCR models run on CPU; bounded threads avoid oversubscribing the server.
            self.engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
        except Exception as exc:
            raise OCRUnavailable(_INSTALL) from exc

    def read_text(self, frame: bytes) -> list[TextBox]:
        import numpy as np

        with Image.open(io.BytesIO(frame)) as image:
            width, height = image.size
            pixels = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()  # RapidOCR expects BGR.
        result, _ = self.engine(pixels)
        return _text_boxes(result, width, height)


@lru_cache(maxsize=2)
def _read_cached(frame: bytes) -> tuple[TextBox, ...]:
    global _backend
    if _backend is None:
        _backend = _RapidOCRBackend()
    try:
        return tuple(_backend.read_text(frame))
    except OCRError:
        raise
    except Exception as exc:
        raise OCRError("OCR 识别失败，请重新观察屏幕后再试") from exc


def read_text(frame: bytes) -> list[TextBox]:
    """Read a JPEG/PNG frame; reuse two recent frames and serialize engine access."""
    if not isinstance(frame, bytes) or not frame:
        raise ValueError("OCR 需要非空屏幕图像")
    with _lock:
        return list(_read_cached(frame))


def find_matches(frame: bytes, query: str, exact: bool = False) -> list[TextBox]:
    """Rank exact labels first, then shorter substring matches; never fuzzy-match a tap."""
    needle = normalize_text(query)
    if not needle:
        raise ValueError("要查找的屏幕文字不能为空")
    matches = []
    for item in read_text(frame):
        label = normalize_text(item.text)
        if label == needle or (not exact and needle in label):
            matches.append(item)
    return sorted(matches, key=lambda item: (
        normalize_text(item.text) != needle, len(normalize_text(item.text)),
        -item.conf, item.box[1], item.box[0],
    ))


def find_text(frame: bytes, query: str, exact: bool = False) -> TextBox | None:
    matches = find_matches(frame, query, exact)
    return matches[0] if matches else None


async def _lookup_async(function, frame: bytes, query: str, exact: bool):
    global _lookup
    with _lookup_lock:
        if _lookup is not None and not _lookup.done.is_set():
            raise OCRError("OCR 正忙，请稍后重新观察屏幕后再试")
        job = _lookup = _Lookup()

        def run():
            try:
                job.result = function(frame, query, exact)
            except BaseException as exc:
                job.error = exc
            finally:
                job.done.set()

        threading.Thread(target=run, name="ocr-lookup", daemon=True).start()
    # Cancellation stops this waiter; the one bounded worker may finish warming the cache.
    while not job.done.is_set():
        await asyncio.sleep(.01)
    if job.error is not None:
        raise job.error
    return job.result


async def find_matches_async(frame: bytes, query: str, exact: bool = False) -> list[TextBox]:
    """Run a lookup without blocking the event loop or queuing stale observations."""
    return await _lookup_async(find_matches, frame, query, exact)


async def find_text_async(frame: bytes, query: str, exact: bool = False) -> TextBox | None:
    return await _lookup_async(find_text, frame, query, exact)
