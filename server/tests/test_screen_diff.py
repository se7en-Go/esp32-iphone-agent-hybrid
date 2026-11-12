import asyncio
import io
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from hidagent import screen_diff


def frame(color="white", boxes=(), size=(180, 390), quality=None):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.rectangle(box, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG" if quality else "PNG", **({"quality": quality} if quality else {}))
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch):
    monkeypatch.setattr(screen_diff, "_worker", screen_diff._TextWorker())
    monkeypatch.setattr(screen_diff.ocr, "available", lambda: False)


def test_pixel_regions_are_normalized_and_fraction_tracks_changed_area():
    result = asyncio.run(screen_diff.summarize(frame(), frame(boxes=[(18, 39, 53, 116), (126, 273, 143, 311)])))
    assert result["status"] == "ok" and not result["size_changed"]
    assert result["changed_fraction"] == pytest.approx(.05, abs=.002)
    assert len(result["changed_regions"]) == 2
    large = result["changed_regions"][0]
    assert large == pytest.approx([.1, .09, .3, .31], abs=.02)
    assert result["text_status"] == "unavailable"
    assert result["texts_appeared"] is None


def test_pixel_comparison_preserves_equal_luminance_color_changes():
    result = asyncio.run(screen_diff.summarize(frame((255, 0, 0)), frame((0, 130, 0))))
    assert result["changed_fraction"] == 1
    assert result["changed_regions"] == [[0, 0, 1, 1]]


def test_small_compression_noise_is_not_a_change():
    result = asyncio.run(screen_diff.summarize(frame((150, 150, 150)), frame((156, 149, 150))))
    assert result["changed_fraction"] == 0
    assert result["changed_regions"] == []


def test_size_change_is_explicit_and_covers_whole_screen():
    result = asyncio.run(screen_diff.summarize(frame(), frame(size=(390, 180))))
    assert result["size_changed"] is True
    assert result["changed_fraction"] == 1
    assert result["changed_regions"] == [[0, 0, 1, 1]]


@pytest.mark.parametrize("before,after,fresh,status", [
    (None, frame(), True, "missing"), (frame(), None, True, "missing"),
    (b"broken", frame(), True, "invalid"), (frame(), b"broken", True, "invalid"),
    (frame(), frame("black"), False, "stale"),
])
def test_unknown_frames_do_not_claim_no_change(before, after, fresh, status):
    result = asyncio.run(screen_diff.summarize(before, after, fresh=fresh))
    assert result["status"] == status
    assert result["changed_fraction"] is None
    assert result["texts_appeared"] is result["texts_disappeared"] is None


def test_identical_frame_needs_no_ocr(monkeypatch):
    monkeypatch.setattr(screen_diff.ocr, "read_text", lambda _: pytest.fail("OCR should not run"))
    picture = frame()
    result = asyncio.run(screen_diff.summarize(picture, picture))
    assert result["text_status"] == "unchanged"
    assert result["texts_appeared"] == result["texts_disappeared"] == []


def test_regions_are_capped_without_losing_remaining_area():
    boxes = [(x, y, x + 5, y + 5) for y in range(12, 380, 24) for x in range(12, 170, 24)]
    result = asyncio.run(screen_diff.summarize(frame(), frame(boxes=boxes)))
    regions = result["changed_regions"]
    assert len(regions) == screen_diff.MAX_REGIONS
    for x, y, _, _ in boxes:
        assert any(left <= x / 180 <= right and top <= y / 390 <= bottom for left, top, right, bottom in regions)


def test_fake_ocr_diff_normalizes_text_and_counts_duplicates(monkeypatch):
    before, after = frame(), frame("black")
    calls = []

    def read(picture):
        calls.append(picture)
        texts = ["Ａ Ｂ", "保存", "保存", "旧文字"] if picture == before else ["ab", "保存", "新文字"]
        return [SimpleNamespace(text=text) for text in texts]

    monkeypatch.setattr(screen_diff.ocr, "available", lambda: True)
    monkeypatch.setattr(screen_diff.ocr, "read_text", read)
    result = asyncio.run(screen_diff.summarize(before, after))
    assert result["text_status"] == "available"
    assert result["texts_appeared"] == ["新文字"]
    assert result["texts_disappeared"] == ["保存", "旧文字"]
    cached = asyncio.run(screen_diff.summarize(before, after))
    assert cached == result and len(calls) == 2


def test_ocr_can_be_disabled_and_backend_failure_does_not_lose_pixels(monkeypatch):
    monkeypatch.setattr(screen_diff.ocr, "available", lambda: True)

    def broken(_):
        raise RuntimeError("broken backend")

    monkeypatch.setattr(screen_diff.ocr, "read_text", broken)
    result = asyncio.run(screen_diff.summarize(frame(), frame("black"), ocr_enabled=False))
    assert result["text_status"] == "disabled" and result["changed_fraction"] == 1
    result = asyncio.run(screen_diff.summarize(frame(), frame("black")))
    assert result["text_status"] == "error" and result["changed_fraction"] == 1


def test_slow_ocr_times_out_without_queueing_or_blocking_asyncio_shutdown(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(screen_diff, "OCR_BUDGET_SECONDS", .025)
    monkeypatch.setattr(screen_diff.ocr, "available", lambda: True)

    def slow(picture):
        calls.append(picture)
        entered.set()
        assert release.wait(2)
        return []

    monkeypatch.setattr(screen_diff.ocr, "read_text", slow)
    worker = screen_diff._worker
    started = time.monotonic()
    try:
        result = asyncio.run(screen_diff.summarize(frame(), frame("black")))
        assert entered.is_set() and result["text_status"] == "timeout"
        assert time.monotonic() - started < .3
        job = worker._active
        busy = asyncio.run(screen_diff.summarize(frame("red"), frame("blue")))
        assert busy["text_status"] == "busy" and len(calls) == 1
    finally:
        release.set()
        active = worker._active
        if active:
            assert active.done.wait(2)
    assert job.done.is_set()
    result = asyncio.run(screen_diff.summarize(frame(), frame("black")))
    assert result["text_status"] == "available" and len(calls) == 2


def test_cancelling_diff_does_not_wait_for_native_ocr(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(screen_diff.ocr, "available", lambda: True)

    def slow(_):
        entered.set()
        assert release.wait(2)
        return []

    monkeypatch.setattr(screen_diff.ocr, "read_text", slow)

    async def scenario():
        task = asyncio.create_task(screen_diff.summarize(frame(), frame("black")))
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    worker = screen_diff._worker
    started = time.monotonic()
    try:
        asyncio.run(scenario())
        assert time.monotonic() - started < .3
    finally:
        release.set()
        active = worker._active
        if active:
            assert active.done.wait(2)
