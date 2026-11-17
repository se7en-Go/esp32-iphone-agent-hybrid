"""One passive later Spotlight observation, never a second shortcut or blind input."""

import asyncio
import io
import time
from dataclasses import replace

import pytest
from fake_clock import ManualClock
from PIL import Image
from test_app_search_home import setup
from test_controls import jpeg
from test_spotlight_field_selection import spotlight, system_labels

from hidagent import feedback, ocr
from hidagent.phone import AppSearchError


def _setup_with_frame_clock(tmp_path, monkeypatch, *, on_home=True):
    # Adjacent synthetic frames need distinct wall-clock stamps even when
    # the host clock ticks coarsely (~15.6 ms on Windows); keep monotonic
    # time real so asyncio.wait_for timeout tests keep their real budgets.
    clock = ManualClock()
    monkeypatch.setattr(time, "time", clock.time)
    phone, device, link, screen = setup(tmp_path, monkeypatch, on_home=on_home)
    publish = screen._on_jpeg

    def next_frame(image):
        clock.advance(.01)
        publish(image)

    monkeypatch.setattr(screen, "_on_jpeg", next_frame)
    return phone, device, link, screen


def rig(tmp_path, monkeypatch, *, first_focused=False, first_labels=None, later=None, later_labels=None):
    phone, device, link, screen = _setup_with_frame_clock(tmp_path, monkeypatch, on_home=True)
    first = spotlight(caret=first_focused, extra=True)
    later = spotlight(caret=True, extra=True) if later is None else later
    events = []

    async def opened(*_args):
        screen._on_jpeg(first)
        return first

    async def labels(frame):
        first_read = not any(event[0] == "ocr" for event in events)
        events.append(("ocr", frame))
        result = first_labels if first_read else later_labels
        return system_labels() if result is None else result

    async def fresh(since, timeout):
        events.append(("passive", since, timeout))
        screen._on_jpeg(later)
        return later

    monkeypatch.setattr(feedback, "wait_after_action", opened)
    monkeypatch.setattr(ocr, "read_text_async", labels)
    monkeypatch.setattr(screen, "fresh", fresh)
    return phone, device, link, screen, events


@pytest.mark.parametrize("suggestions_changed", [False, True])
def test_later_visible_focus_uses_one_original_shortcut_and_its_own_field(tmp_path, monkeypatch, suggestions_changed):
    async def scenario():
        later = spotlight(caret=True, extra=True, edit="sibling" if suggestions_changed else None)
        phone, device, link, _, events = rig(tmp_path, monkeypatch, later=later)
        try:
            region = await phone.open_app_search("Safari")
            assert region[1] < .54 < .56 < region[3]
            assert [event[0] for event in events] == ["ocr", "passive", "ocr"]
            assert events[-1][1] == later
            assert link.written == [b"1 key cmd+space\n", b"2 type Safari\n"]
        finally:
            device.close()
    asyncio.run(scenario())


def test_already_valid_first_observation_keeps_single_read_and_no_added_wait(tmp_path, monkeypatch):
    async def scenario():
        phone, device, link, _, events = rig(tmp_path, monkeypatch, first_focused=True)
        try:
            await phone.open_app_search("Safari")
            assert [event[0] for event in events] == ["ocr"]
            assert link.written == [b"1 key cmd+space\n", b"2 type Safari\n"]
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["suggestions", "field", "labels", "again", "generation"])
def test_focused_spotlight_rechecks_one_newer_frame_before_typing(tmp_path, monkeypatch, change):
    async def scenario():
        phone, device, link, screen = _setup_with_frame_clock(tmp_path, monkeypatch, on_home=True)
        first = spotlight(extra=True)
        later = spotlight(extra=True, edit="field" if change == "field" else "sibling")
        inspected = []

        async def opened(*_args):
            screen._on_jpeg(first)
            return first

        async def labels(frame):
            inspected.append(frame)
            if frame == first:
                if change == "generation":
                    screen._on_ready()
                screen._on_jpeg(later)
            elif change == "again":
                screen._on_jpeg(spotlight(extra=True, edit="keyboard"))
            return system_labels(marker=change != "labels" or frame == first)

        monkeypatch.setattr(feedback, "wait_after_action", opened)
        monkeypatch.setattr(ocr, "read_text_async", labels)
        try:
            if change == "suggestions":
                field = await phone.open_app_search("Settings")
                assert field[1] < .54 < .56 < field[3]
                assert link.written == [b"1 key cmd+space\n", b"2 type Settings\n"]
            else:
                with pytest.raises(AppSearchError):
                    await phone.open_app_search("Settings")
                assert link.written == [b"1 key cmd+space\n"]
            assert inspected == ([first] if change == "generation" else [first, later])
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("labels", [[], system_labels(marker=False), system_labels(placeholder="missing"),
                                     system_labels(marker="below")])
def test_first_frame_without_spotlight_identity_never_waits_for_a_replacement(tmp_path, monkeypatch, labels):
    async def scenario():
        phone, device, link, _, events = rig(tmp_path, monkeypatch, first_labels=labels)
        try:
            with pytest.raises(AppSearchError):
                await phone.open_app_search("Safari")
            assert [event[0] for event in events] == ["ocr"]
            assert link.written == [b"1 key cmd+space\n"]
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["unrelated", "marker_moved", "placeholder_moved", "label_changed",
                                 "weak_confidence", "still_dark", "timeout", "same_receipt", "dimensions",
                                 "stale"])
def test_later_missing_or_different_focus_proof_never_types_or_retries(tmp_path, monkeypatch, case):
    async def scenario():
        labels = system_labels()
        if case in {"marker_moved", "placeholder_moved", "label_changed", "weak_confidence"}:
            index = 1 if case == "marker_moved" else 0
            hit = labels[index]
            box = tuple(value + (.05 if i % 2 else 0) for i, value in enumerate(hit.box))
            labels[index] = ocr.TextBox("Search" if case == "label_changed" else hit.text,
                                        box if case.endswith("moved") else hit.box, hit.center,
                                        .79 if case == "weak_confidence" else hit.conf)
        later = jpeg("black") if case == "unrelated" else spotlight(caret=case != "still_dark", extra=True)
        phone, device, link, screen, events = rig(tmp_path, monkeypatch, later=later,
                                                 later_labels=[] if case == "unrelated" else labels)
        if case in {"timeout", "same_receipt"}:
            async def fresh(since, timeout):
                events.append(("passive", since, timeout))
                return None if case == "timeout" else screen.frame
            monkeypatch.setattr(screen, "fresh", fresh)
        elif case in {"dimensions", "stale"}:
            original_fresh = screen.fresh

            async def changed(since, timeout):
                result = await original_fresh(since, timeout)
                if case == "dimensions":
                    output = io.BytesIO()
                    Image.open(io.BytesIO(result)).resize((500, 1100)).save(output, "PNG")
                    screen._on_jpeg(output.getvalue())
                else:
                    screen._snapshot = replace(screen.snapshot, updated=time.time()-6)
                return screen.frame

            monkeypatch.setattr(screen, "fresh", changed)
        try:
            with pytest.raises(AppSearchError):
                await phone.open_app_search("Safari")
            assert sum(event[0] == "passive" for event in events) == 1
            assert sum(event[0] == "ocr" for event in events) <= 2
            assert link.written == [b"1 key cmd+space\n"]
        finally:
            device.close()
    asyncio.run(scenario())


def test_passive_wait_has_its_own_finite_timeout_without_late_input(tmp_path, monkeypatch):
    async def scenario():
        from hidagent import phone as phone_module

        phone, device, link, screen, _ = rig(tmp_path, monkeypatch)
        cancelled = []

        async def hung(since, timeout):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        monkeypatch.setattr(screen, "fresh", hung)
        monkeypatch.setattr(phone_module, "SPOTLIGHT_FOCUS_WAIT", .01)
        try:
            with pytest.raises(AppSearchError):
                await phone.open_app_search("Safari")
            assert cancelled == [True]
            assert link.written == [b"1 key cmd+space\n"]
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["link", "generation", "abort", "device"])
def test_original_session_is_kept_across_the_passive_wait(tmp_path, monkeypatch, change):
    async def scenario():
        phone, device, link, screen, _ = rig(tmp_path, monkeypatch)
        fresh = screen.fresh

        async def changed(since, timeout):
            result = await fresh(since, timeout)
            if change == "link":
                device.links += 1
            elif change == "generation":
                screen._on_ready()
                screen._on_jpeg(result)
            elif change == "abort":
                device._abort_epoch += 1
            else:
                from types import SimpleNamespace
                phone.device = SimpleNamespace(links=device.links, abort_epoch=device.abort_epoch,
                                               connected=True, board_status={"hid": {"path": "ble"}})
            return result

        monkeypatch.setattr(screen, "fresh", changed)
        try:
            with pytest.raises(AppSearchError):
                await phone.open_app_search("Safari")
            assert link.written == [b"1 key cmd+space\n"]
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["layout", "link", "generation", "abort"])
def test_second_observation_still_guards_the_actual_queued_query_write(tmp_path, monkeypatch, change):
    async def scenario():
        phone, device, link, screen, events = rig(tmp_path, monkeypatch)
        read = ocr.read_text_async

        async def hold(frame):
            result = await read(frame)
            if sum(event[0] == "ocr" for event in events) == 2:
                await device._lock.acquire()
            return result

        monkeypatch.setattr(ocr, "read_text_async", hold)
        operation = asyncio.create_task(phone.open_app_search("Safari"))
        try:
            async def waiting():
                while not device._lock._waiters:
                    if operation.done():
                        await operation
                    await asyncio.sleep(0)
            await asyncio.wait_for(waiting(), 2)
            if change == "layout":
                screen._on_jpeg(jpeg("black"))
            elif change == "link":
                device.links += 1
            elif change == "generation":
                screen._on_ready()
                screen._on_jpeg(screen.frame)
            else:
                device._abort_epoch += 1
            device._lock.release()
            with pytest.raises(AppSearchError):
                await operation
            assert link.written == [b"1 key cmd+space\n"]
        finally:
            if device._lock.locked():
                device._lock.release()
            if not operation.done():
                operation.cancel()
            device.close()
    asyncio.run(scenario())
