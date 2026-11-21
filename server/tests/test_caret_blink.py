"""Caret-only animation needs bounded field and reversible, recent visual evidence."""

import asyncio
import io
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from hidagent import screen_compare
from hidagent.freshness import snapshot
from hidagent.input_context import _focused_region
from hidagent.screen import FrameSnapshot, Screen
from hidagent.text_target import TargetChangedError, TextTargetGuard


def spotlight_frame(level=0, *, edit=None, bounded=True):
    # Geometry/colors match the actual failed 500x1080 Spotlight frame; no user pixels.
    image = Image.new("RGB", (500, 1080), (110, 45, 74))
    draw = ImageDraw.Draw(image)
    if bounded:
        draw.rounded_rectangle((20, 565, 480, 630), radius=18, fill=(169, 126, 156))
    else:
        draw.rectangle((0, 0, 499, 1079), fill=(169, 126, 156))
    draw.text((80, 590), "Search", fill=(240, 225, 238))
    color = tuple(round(a + level * (b - a)) for a, b in zip((169, 126, 156), (255, 247, 255), strict=True))
    draw.rounded_rectangle((68, 581, 71, 614), radius=1, fill=color)
    if edit == "text":
        draw.text((140, 590), "edited", fill="white")
    elif edit == "near":
        draw.rectangle((74, 582, 77, 610), fill="white")
    elif edit == "outside":
        draw.rectangle((250, 200, 270, 220), fill="white")
    elif edit == "wide":
        draw.rectangle((68, 581, 80, 614), fill="white")
    data = io.BytesIO()
    image.save(data, "JPEG", quality=90)
    return data.getvalue()


def history(*levels, bounded=True, edit=None):
    return tuple(SimpleNamespace(frame=spotlight_frame(v, bounded=bounded, edit=edit), arrived=i * .4)
                 for i, v in enumerate(levels))


def test_only_periodic_caret_blink_is_ignored():
    frames = history(0, 1, 0, 1)
    before, after = spotlight_frame(.3), spotlight_frame(1)
    assert not screen_compare.observation_changed(before, after, caret_frames=frames)


def test_caret_search_does_not_floodfill_unchanged_distant_fields(monkeypatch):
    before = spotlight_frame(1)
    image = Image.open(io.BytesIO(before)).convert("RGB")
    ImageDraw.Draw(image).rectangle((250, 200, 252, 230), fill="white")
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    seeds = []
    floodfill = ImageDraw.floodfill

    def record(image, seed, value, **kwargs):
        seeds.append(seed)
        return floodfill(image, seed, value, **kwargs)

    monkeypatch.setattr(ImageDraw, "floodfill", record)
    assert screen_compare.observation_changed(before, output.getvalue(), caret_frames=history(0, 1, 0, 1))
    assert all(abs(x - 250) < 15 for x, _ in seeds)


@pytest.mark.parametrize("short_shape", [(69, 588, 70, 608), (68, 581, 71, 590), (68, 606, 71, 614)])
@pytest.mark.parametrize("reverse", [False, True])
def test_old_blink_evidence_does_not_hide_a_new_partial_character(short_shape, reverse):
    image = Image.open(io.BytesIO(spotlight_frame(0))).convert("RGB")
    ImageDraw.Draw(image).rectangle(short_shape, fill="white")
    data = io.BytesIO()
    image.save(data, "JPEG", quality=90)
    before, after = spotlight_frame(1), data.getvalue()
    if reverse:
        before, after = after, before
    assert screen_compare.observation_changed(before, after)
    assert screen_compare.observation_changed(before, after, caret_frames=history(0, 1, 0, 1))


@pytest.mark.parametrize("levels", [(0, 1), (0, 0, 1, 1), (1, 1, 0), (.4, 1, .4, 1)])
def test_new_letter_or_single_transition_is_not_a_blink(levels):
    assert screen_compare.observation_changed(spotlight_frame(0), spotlight_frame(1),
                                              caret_frames=history(*levels))


@pytest.mark.parametrize("edit", ["text", "near", "outside", "wide"])
def test_real_edits_are_not_masked_by_caret_evidence(edit):
    assert screen_compare.observation_changed(spotlight_frame(0), spotlight_frame(1, edit=edit),
                                              caret_frames=history(0, 1, 0, 1))


def test_periodic_line_without_bounded_field_is_not_a_caret():
    assert screen_compare.observation_changed(spotlight_frame(0, bounded=False),
                                              spotlight_frame(1, bounded=False),
                                              caret_frames=history(0, 1, 0, 1, bounded=False))


def test_history_of_other_field_text_does_not_authorize_current_caret():
    assert screen_compare.observation_changed(spotlight_frame(0), spotlight_frame(1),
                                              caret_frames=history(0, 1, 0, 1, edit="text"))


def test_white_focus_is_opt_in_for_verified_spotlight():
    assert _focused_region(spotlight_frame(1)) is None
    region = _focused_region(spotlight_frame(1), allow_white=True)
    assert region and region[0] < .136 < region[2] and region[1] < .55 < region[3]


def test_text_target_guard_keeps_caret_evidence_and_rejects_edits():
    screen = SimpleNamespace(frame=spotlight_frame(1), live=True, updated=time.time(), generation=1,
                             recent_frames=history(0, 1, 0, 1))
    guard = TextTargetGuard(screen, spotlight_frame(.3), SimpleNamespace(center=(.61, .159)))
    guard()
    screen.frame = spotlight_frame(1, edit="text")
    with pytest.raises(TargetChangedError):
        guard()


def test_recent_frame_ring_is_bounded_same_generation_and_expires():
    screen = Screen()
    now = time.monotonic()
    for i in range(80):
        screen._remember_snapshot(FrameSnapshot(b"frame", None, now - .79 + i / 100, generation=1, observed_live=True))
    screen.generation = 1
    assert len(screen.recent_frames) == 32
    screen._remember_snapshot(FrameSnapshot(b"new", None, now, generation=2, observed_live=True))
    screen.generation = 2
    assert [item.frame for item in screen.recent_frames] == [b"new"]
    screen._remember_snapshot(FrameSnapshot(b"old", None, now - 10, generation=3, observed_live=True))
    screen.generation = 3
    assert not screen.recent_frames
    assert not screen._recent_frames


def test_mac_ipc_publishes_history_once_per_received_frame_and_discards_reconnect():
    from hidagent.screen_uxplay import UxPlayScreen

    screen = UxPlayScreen(socket_path="/not-used")
    metadata = {"instance": "local-test", "generation": 1, "running": True, "live": True,
                "arrived": time.monotonic(), "updated": time.time()}
    screen._accept_remote(metadata, b"one")
    screen._accept_remote(metadata, b"one")
    assert len(screen.recent_frames) == 1
    screen._remote_disconnected()
    assert not screen.recent_frames
    screen._accept_remote({**metadata, "generation": 2}, b"two")
    assert [frame.frame for frame in screen.recent_frames] == [b"two"]


def test_controls_and_agent_context_accept_only_caret_blink(monkeypatch):
    from test_agent_text import setup

    from hidagent.agent import ObservationContentChanged
    from hidagent.control import Controls

    agent, phone, _ = setup()
    screen = SimpleNamespace(frame=spotlight_frame(.3), live=True, updated=time.time(), generation=1,
                             recent_frames=history(0, 1, 0, 1))
    controls = Controls(phone, screen)
    token = controls.observation()["screen_id"]
    action = {"action": "key", "combo": "esc", "_observation": snapshot(screen), "_shot": screen.frame}
    agent.screen = screen
    screen.frame = spotlight_frame(1)
    controls.consume(token)
    agent._validate_context(action)
    screen.frame = spotlight_frame(1, edit="outside")
    with pytest.raises(ObservationContentChanged):
        agent._validate_context(action)


@pytest.mark.parametrize("case", ["ready", "dark_phase", "other_app", "missing_placeholder", "outside_placeholder"])
def test_spotlight_white_focus_requires_matching_ocr_before_typing(case, tmp_path, monkeypatch):
    from test_assistive_home import frame as home_frame
    from test_control_input_context import Link

    from hidagent import feedback, freshness, ocr
    from hidagent.device import Device
    from hidagent.phone import AppSearchError, Phone

    monkeypatch.setenv("HIDAGENT_SPOTLIGHT_BLE_VERIFIED", "1")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)

    async def scenario():
        device, link, screen = Device(), Link(), Screen()
        device.attach(link)
        device.board_status = {"hid": {"path": "ble"}}
        screen._on_jpeg(home_frame(home=True))
        phone = Phone(device, tmp_path / "calibration.json", screen)

        async def wait_after_action(_screen, _when):
            now = time.monotonic()
            wall = time.time()
            screen._recent_frames.clear()  # Seed the sequence the mocked wait would have received.
            for i, level in enumerate((0, 1, 0, 1)):
                screen._remember_snapshot(FrameSnapshot(spotlight_frame(level), None,
                    now - 1.3 + .4 * i, updated=wall - 1.3 + .4 * i,
                    generation=screen.generation, observed_live=True))
            screen._on_jpeg(spotlight_frame(0 if case == "dark_phase" else 1))
            return screen.frame

        async def read_labels(_frame):
            field_box = (.16, .54, .29, .56) if case != "outside_placeholder" else (.16, .2, .29, .24)
            return [ocr.TextBox("Search" if case != "missing_placeholder" else "Hello", field_box, (.2, .55), .99),
                    ocr.TextBox("Other App" if case == "other_app" else "Siri Suggestions",
                                (.1, .1, .4, .15), (.25, .12), .99)]

        monkeypatch.setattr(feedback, "wait_after_action", wait_after_action)
        monkeypatch.setattr(ocr, "read_text_async", read_labels)
        try:
            if case in {"ready", "dark_phase"}:
                await phone.open_app_search("Settings")
                assert link.written == [b"1 key cmd+space\n", b"2 type Settings\n"]
            else:
                with pytest.raises(AppSearchError):
                    await phone.open_app_search("Settings")
                assert link.written == [b"1 key cmd+space\n"]
        finally:
            device.close()

    asyncio.run(scenario())


def test_white_page_field_carets_skips_giant_background_fast():
    import time

    from PIL import Image

    from hidagent.caret import field_carets

    started = time.perf_counter()
    result = field_carets(Image.new("RGB", (720, 1560), "white"), allow_white=True)
    elapsed = time.perf_counter() - started
    assert result == []
    assert elapsed < 2.0, f"full white page caret scan took {elapsed:.2f}s"
