"""Regression 2: a bare tap re-checks the MEASURED landing before sending.

The request may be outside the keyboard suggestion bar while the pointer
drifted into it during the move. The pre-move guard only sees the request,
so tap_verified must re-check the measured landing against the latest
frame just before sending. Request outside the bar + landing inside the
bar must send zero clicks.
"""
import asyncio
import pathlib
import time

import pytest
from test_pointer_session import setup_phone

from hidagent.text_target import PointerLandingError

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "suggestion_bar"


def test_bare_tap_request_outside_landing_inside_sends_zero_clicks(tmp_path):
    phone, device, mirror = setup_phone(tmp_path)
    keyboard = (FIXTURES / "keyboard.jpg").read_bytes()
    mirror.frame = keyboard
    mirror.updated = time.time()
    # The request is just above the suggestion bar (bar is 0.579-0.639),
    # so the pre-move guard passes. The pointer actually lands inside it.
    requested = (0.5, 0.55)
    landed = (0.5, 0.60)

    async def measured_landing(x, y, **kwargs):
        phone._pointer_confirmed = True
        return landed

    async def no_effect(*args, **kwargs):
        return False, True

    phone.point_verified = measured_landing
    phone._tap_effect = no_effect

    with pytest.raises(PointerLandingError, match="实测落点在键盘建议栏"):
        asyncio.run(phone.tap_verified(*requested))

    assert device.commands.count("click") == 0
    assert "click" not in device.commands


def test_bare_tap_request_and_landing_outside_sends_one_click(tmp_path):
    # Control case: both the request and the measured landing are outside
    # the bar, so the click is sent.
    phone, device, mirror = setup_phone(tmp_path)
    keyboard = (FIXTURES / "keyboard.jpg").read_bytes()
    mirror.frame = keyboard
    mirror.updated = time.time()
    requested = (0.5, 0.45)
    landed = (0.5, 0.45)

    async def measured_landing(x, y, **kwargs):
        phone._pointer_confirmed = True
        return landed

    async def no_effect(*args, **kwargs):
        return False, True

    phone.point_verified = measured_landing
    phone._tap_effect = no_effect

    result = asyncio.run(phone.tap_verified(*requested))
    assert result["clicked"] is True
    assert device.commands.count("click") == 1
