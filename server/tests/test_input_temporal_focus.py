"""Only past, same-session blink evidence may establish the original input field."""

import io

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_caret_temporal import search_frame

from hidagent.freshness import require_fresh
from hidagent.input_context import InputContextGuard
from hidagent.screen import Screen


@pytest.fixture
def source(monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)
    screen = Screen()
    screen._on_ready()
    return clock, screen


def publish_cycle(clock, screen, *, text="a"):
    for level in (1, 0, 1, 0):
        clock.advance(.4)
        screen._on_jpeg(search_frame(level, text=text))


def observe(clock, screen, **kwargs):
    clock.advance(.1)
    screen._on_jpeg(search_frame(.25, **kwargs))
    return require_fresh(screen)


def test_temporal_proof_establishes_original_field_even_if_guard_is_created_after_typing(source):
    clock, screen = source
    publish_cycle(clock, screen)
    original = observe(clock, screen)
    clock.advance(.1)
    screen._on_jpeg(search_frame(.25, text="b"))
    guard = InputContextGuard(screen, original)
    assert guard.region is not None
    assert guard(stage="accept").frame == screen.frame
    assert guard(stage="chunk").frame == screen.frame
    with pytest.raises(RuntimeError, match="changed"):
        guard(stage="context")  # the first write still needs the original whole-page context


@pytest.mark.parametrize("kind", ["candidate", "outside"])
def test_temporal_field_never_exempts_keyboard_candidates_or_other_content(source, kind):
    clock, screen = source
    publish_cycle(clock, screen)
    original = observe(clock, screen)
    guard = InputContextGuard(screen, original)
    assert guard.region is not None
    image = Image.open(io.BytesIO(search_frame(.25, text="b"))).convert("RGB")
    area = (10, 645, 120, 680) if kind == "candidate" else (100, 250, 120, 270)
    ImageDraw.Draw(image).rectangle(area, fill=(15, 15, 18))
    encoded = io.BytesIO()
    image.save(encoded, "PNG")
    clock.advance(.1)
    screen._on_jpeg(encoded.getvalue())
    with pytest.raises(RuntimeError, match="changed"):
        guard(stage="accept")


@pytest.mark.parametrize("kind", ["expired", "other_text", "future", "other_generation", "incomplete"])
def test_no_focus_from_expired_future_reconnected_or_unrelated_history(source, kind):
    clock, screen = source
    if kind == "future":
        original = observe(clock, screen)
        publish_cycle(clock, screen)
    else:
        if kind == "incomplete":
            for level in (0, 1):
                clock.advance(.4)
                screen._on_jpeg(search_frame(level))
        else:
            publish_cycle(clock, screen, text="b" if kind == "other_text" else "a")
        if kind == "expired":
            clock.advance(4.1)
        if kind == "other_generation":
            original = observe(clock, screen)
            screen._on_ready()
            publish_cycle(clock, screen)
        else:
            original = observe(clock, screen)
    guard = InputContextGuard(screen, original)
    assert guard.region is None
