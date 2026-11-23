"""Only an independently proved measurement may carry quiet across publications."""

import asyncio
import base64
import io
import time

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_host_observed_click import setup

from hidagent import freshness, screen_diff
from hidagent.control import Controls
from hidagent.phone import _PointerMeasurement


def frame(version, *, page="white"):
    image = Image.new("RGB", (160, 260), page)
    ImageDraw.Draw(image).ellipse((73, 123, 87, 137), fill="gray")
    image.putpixel((10, 95), (240 + version % 10,) * 3)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    return encoded.getvalue()


@pytest.mark.parametrize("transition", ["noise", "same_pixels", "page", "input", "deadline"])
def test_point_measurement_survives_only_proved_current_publications(tmp_path, monkeypatch, transition):
    clock = ManualClock()
    clock.install(monkeypatch)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
    phone, device, screen, _ = setup(tmp_path)
    controls = Controls(phone, screen)
    source = frame(0)
    screen._on_jpeg(source)
    measurement = _PointerMeasurement(
        (.5, .5), freshness.snapshot(screen), device, device.links, device.abort_epoch,
        device.input_sequence, device.last_input_at, device.session_started_at, None, time.monotonic())
    phone._point_measurement = measurement
    controls.observation()
    clock.advance(.02)
    screen._on_jpeg(frame(1))

    observation = controls.observation
    issued = []

    def issue(accepted, **kwargs):
        shot = observation(accepted, **kwargs)
        issued.append(accepted)
        if transition == "deadline" or len(issued) == 1 or (transition == "noise" and len(issued) <= 6):
            # Publication continues while the previous endpoint is being qualified.
            clock.advance(.14)
            if transition == "input":
                device.input_sequence += 1
            image = (accepted.frame if transition == "same_pixels"
                     else frame(len(issued) + 1, page="blue" if transition == "page" else "white"))
            screen._on_jpeg(image)
        return shot

    monkeypatch.setattr(controls, "observation", issue)
    began = time.perf_counter()
    result = asyncio.run(controls._respond(
        source, device.last_input_at, began, began, True, None,
        {"_expected_measurement": measurement.id, "pointer_seen": False, "measured_pointer": None}))

    if transition in {"noise", "same_pixels"}:
        assert result["fresh"] and result["pointer_measurement_id"] == measurement.id
        latest = freshness.snapshot(screen)
        assert controls._observation == latest and result["frame_received_at"] == latest.updated
        assert base64.b64decode(result["image_base64"]) == latest.frame == controls.ticket[2]
        assert result["screen_diff"] == asyncio.run(screen_diff.summarize(
            source, latest.frame, fresh=True, ocr_enabled=False))
        assert clock.elapsed < 2.0
    else:
        assert not result["fresh"] and result["screen_id"] is None and controls.ticket is None
        assert "pointer_measurement_id" not in result and not result["pointer_seen"]
        assert result["screen_diff"]["status"] == "stale"
        assert clock.elapsed <= 2.16  # At most the last in-flight validation beyond the deadline.
