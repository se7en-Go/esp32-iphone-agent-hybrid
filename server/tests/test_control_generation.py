"""An OCR selector must retain the observation's original stream identity."""

import asyncio
from types import SimpleNamespace

from test_controls import FakePhone, instant_mirror, jpeg  # noqa: F401

from hidagent import ocr
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.screen import Screen


def test_reconnect_during_selection_cannot_rebind_the_original_target(monkeypatch):
    screen, phone = Screen(), FakePhone()
    frame = jpeg()
    screen._on_jpeg(frame)
    controls = Controls(phone, screen)
    request = ControlAction(action="tap_text", screen_id=controls.observation()["screen_id"], text="设置")
    target = ocr.TextBox("设置", (.1, .2, .5, .3), (.3, .25), .99)
    ocr.set_backend(SimpleNamespace(read_text=lambda _frame: [target]))
    select = ocr.select_match

    def reconnect_after_validation(*args, **kwargs):
        chosen = select(*args, **kwargs)
        # The video thread can reconnect after the post-OCR check and before
        # constructing the final-write target guard, even with identical pixels.
        screen._on_ready()
        screen._on_jpeg(frame)
        return chosen

    monkeypatch.setattr(ocr, "select_match", reconnect_after_validation)
    try:
        result = asyncio.run(controls.perform(request))
    finally:
        ocr.set_backend(None)
    assert screen.generation == 1
    assert phone.calls == []
    assert result["command_acknowledged"] is False
    assert "连接" in result["error"] or "observation" in result["error"]
