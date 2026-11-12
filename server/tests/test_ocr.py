import asyncio
import importlib.util
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from hidagent import ocr

FIXTURES = Path(__file__).parent / "fixtures" / "ios26"
HAS_RAPIDOCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None


@pytest.fixture
def real_ocr():
    if not HAS_RAPIDOCR:
        if os.environ.get("HIDAGENT_REQUIRE_OCR") == "1":
            pytest.fail("OCR validation requires server[ocr]; refusing to skip real fixtures")
        pytest.skip("install server[ocr] to run real OCR")


@pytest.fixture(autouse=True)
def reset_reader():
    ocr.set_backend(None)
    yield
    ocr.set_backend(None)


def box(text, conf=.9, y=.1):
    return ocr.TextBox(text, (.1, y, .4, y + .1), (.25, y + .05), conf)


def test_normalization_preserves_punctuation_but_folds_width_case_and_spacing():
    assert ocr.normalize_text(" Ｗｉ－Ｆｉ\n 设置　１２３ ") == "wi-fi设置123"
    assert ocr.normalize_text("132 6717 9555") == "13267179555"
    assert ocr.normalize_text(" A.B ") == "a.b"


def test_ranked_matches_prefer_exact_then_shorter_labels_and_confidence():
    items = [box("关于本机 设置", .99), box("设置", .6, .4), box("设 置", .9, .6),
             box("设置项", .99), box("设备", .99)]
    ocr.set_backend(SimpleNamespace(read_text=lambda frame: items))
    assert [item.text for item in ocr.find_matches(b"frame", "设置")] == ["设 置", "设置", "设置项", "关于本机 设置"]
    assert ocr.find_text(b"frame", "设置", exact=True) == items[2]
    assert len(ocr.find_matches(b"frame", "设置", exact=True)) == 2
    assert ocr.find_text(b"frame", "missing") is None
    assert ocr.find_text(b"frame", "设备置") is None


def test_duplicate_matches_have_deterministic_reading_order_at_equal_confidence():
    items = [box("无", y=.6), box("无", y=.3)]
    ocr.set_backend(SimpleNamespace(read_text=lambda frame: items))
    assert ocr.find_matches(b"frame", "无") == items[::-1]


def test_empty_query_fails_before_loading_backend(monkeypatch):
    monkeypatch.setattr(ocr, "_RapidOCRBackend", lambda: pytest.fail("must not load OCR"))
    with pytest.raises(ValueError, match="不能为空"):
        ocr.find_text(b"frame", "\n　 ")


def test_available_checks_installation_without_loading_backend(monkeypatch):
    monkeypatch.setattr(ocr.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(ocr, "_RapidOCRBackend", lambda: pytest.fail("must not load OCR"))
    assert not ocr.available()
    ocr.set_backend(SimpleNamespace(read_text=lambda frame: []))
    assert ocr.available()


def test_optional_backend_failure_has_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
    with pytest.raises(ocr.OCRUnavailable, match=r"server\[ocr\]"):
        ocr.read_text(b"frame")


def test_reader_failures_are_actionable_and_not_cached():
    calls = []
    def broken(frame):
        calls.append(frame)
        raise RuntimeError("native inference failure")
    ocr.set_backend(SimpleNamespace(read_text=broken))
    for _ in range(2):
        with pytest.raises(ocr.OCRError, match="识别失败"):
            ocr.read_text(b"frame")
    assert len(calls) == 2


def test_recent_frame_cache_is_bounded_and_returns_independent_lists():
    calls = []
    def read(frame):
        calls.append(frame)
        return [box("设置")]
    ocr.set_backend(SimpleNamespace(read_text=read))
    first = ocr.read_text(b"one")
    first.clear()
    assert ocr.read_text(b"one")
    ocr.read_text(b"two")
    ocr.read_text(b"three")
    ocr.read_text(b"one")
    assert calls == [b"one", b"two", b"three", b"one"]
    ocr.set_backend(SimpleNamespace(read_text=lambda frame: [box("新内容")]))
    assert ocr.read_text(b"one")[0].text == "新内容"


def test_inference_is_serialized_between_action_and_diff_workers():
    active = threading.Lock()
    def read(frame):
        assert active.acquire(blocking=False)
        try:
            time.sleep(.01)
            return [box(frame.decode())]
        finally:
            active.release()
    ocr.set_backend(SimpleNamespace(read_text=read))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(ocr.read_text, [b"one", b"two", b"three"]))
    assert [items[0].text for items in results] == ["one", "two", "three"]


def test_async_lookup_runs_off_loop_and_propagates_errors():
    main_thread = threading.current_thread()
    def read(frame):
        assert threading.current_thread() is not main_thread
        return [box("设置")]
    ocr.set_backend(SimpleNamespace(read_text=read))

    async def scenario():
        assert (await ocr.find_text_async(b"frame", "设置")).text == "设置"
        assert len(await ocr.find_matches_async(b"frame", "设置")) == 1
        assert await ocr.find_text_async(b"frame", "missing") is None
        with pytest.raises(ValueError, match="不能为空"):
            await ocr.find_matches_async(b"frame", " ")
    asyncio.run(scenario())


def test_cancelled_async_lookup_leaves_only_one_worker_and_keeps_loop_responsive():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read(frame):
        calls.append(frame)
        entered.set()
        assert release.wait(2)
        return [box("设置")]
    ocr.set_backend(SimpleNamespace(read_text=read))

    async def scenario():
        pending = asyncio.create_task(ocr.find_matches_async(b"frame", "设置"))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            assert entered.is_set() and not pending.done()
            with pytest.raises(ocr.OCRError, match="正忙"):
                await ocr.find_text_async(b"different frame", "设置")
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            with pytest.raises(ocr.OCRError, match="正忙"):
                await ocr.find_matches_async(b"different frame", "设置")
        finally:
            release.set()
            for _ in range(100):
                if ocr._lookup.done.is_set():
                    break
                await asyncio.sleep(.01)
        assert ocr._lookup.done.is_set()
        assert (await ocr.find_text_async(b"frame", "设置")).text == "设置"
        assert calls == [b"frame"]
    asyncio.run(scenario())


def test_raw_boxes_are_normalized_clipped_and_invalid_results_ignored():
    raw = [([[10, 20], [60, 20], [60, 80], [10, 80]], "设置", .98),
           ([[-5, -2], [110, -2], [110, 220], [-5, 220]], "screen", 1.1),
           ([[10, 10]] * 4, "degenerate", .9),
           ([[0, 0], [10, 0], [10, 10], [0, 10]], " ", .9),
           ([[0, 0], [10, 0], [10, 10], [0, 10]], "bad", float("nan"))]
    result = ocr._text_boxes(raw, 101, 201)
    assert result == [ocr.TextBox("screen", (0, 0, 1, 1), (.5, .5), 1),
                      ocr.TextBox("设置", (.1, .1, .6, .4), (.35, .25), .98)]
    assert ocr._text_boxes(None, 101, 201) == []


@pytest.mark.parametrize("frame", [b"", None, "not bytes"])
def test_invalid_frames_do_not_load_backend(frame):
    with pytest.raises(ValueError, match="非空"):
        ocr.read_text(frame)


def measured_read(frame, label):
    started = time.perf_counter()
    items = ocr.read_text(frame)
    cold_ms = (time.perf_counter() - started) * 1000
    warm_ms = []
    for _ in range(3):
        # Clear only the frame cache, keeping the initialized model for warm measurements.
        ocr._read_cached.cache_clear()
        started = time.perf_counter()
        ocr.read_text(frame)
        warm_ms.append((time.perf_counter() - started) * 1000)
    print(f"{label}: cold first frame {cold_ms:.1f} ms; "
          f"warm uncached median {statistics.median(warm_ms):.1f} ms "
          f"(range {min(warm_ms):.1f}–{max(warm_ms):.1f} ms, n=3)")
    return items


@pytest.mark.ocr
def test_real_settings_frame_reads_chinese_and_english(real_ocr):
    frame = (FIXTURES / "00_home_1500ms.jpg").read_bytes()
    items = measured_read(frame, "Settings 720x1560")
    assert any("辅助触控" in ocr.normalize_text(item.text) for item in items)
    assert any("iphone" in ocr.normalize_text(item.text) for item in items)
    target = ocr.find_text(frame, "自定义顶层菜单", exact=True)
    assert target is not None
    assert .1 < target.center[0] < .5 and .3 < target.center[1] < .36


@pytest.mark.ocr
def test_real_phone_keypad_reads_number_and_chinese_label(real_ocr):
    frame = (FIXTURES / "dial_final.jpg").read_bytes()
    measured_read(frame, "Phone keypad 720x1560")
    number = ocr.find_text(frame, "13267179555", exact=True)
    assert number is not None
    assert .15 < number.center[0] < .85 and .08 < number.center[1] < .16
    target = ocr.find_text(frame, "拨号键盘", exact=True)
    assert target is not None and target.center[1] > .9
