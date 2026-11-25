"""A causal return frame can still show an animating cursor, not a settled landing."""
import asyncio
import io
import time

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_verified_tap import LaggingMirror, calibrated_phone


def replay_probe(tmp_path, monkeypatch, baseline, reference, outward, returned):
    clock = ManualClock()
    clock.install(monkeypatch)

    class Mirror(LaggingMirror):
        generation = 1
        _last_capture_age = .005

        def publish(self, raw):
            self.frame, self.updated = raw, time.time()

        async def send(self, command):
            self.commands.append(command)
            sequence = outward if len(self.commands) == 1 else returned
            for delay, raw in sequence:
                clock.call_later(delay, self.publish, raw)
            await asyncio.sleep(.01)

    mirror = Mirror()
    mirror.frame = baseline
    phone = calibrated_phone(tmp_path, mirror)
    phone.span, phone._pace = {"w": 500, "h": 1080}, 4
    recorded = []
    phone._record_pointer_measurement = lambda xy, source: recorded.append((xy, source))
    monkeypatch.setattr(phone, "_extend_escape_probe", lambda *args: None)
    result = asyncio.run(phone._localize_pointer((.32, .57), 0, reference=(reference, None), probe=(1, -83)))
    assert mirror.commands == ["move 0 -83", "move 0 83"]
    return result, recorded


def scene(y=None, *, edit=False):
    image = Image.new("RGB", (500, 1080), (40, 80, 120))
    draw = ImageDraw.Draw(image)
    if y is not None:
        draw.ellipse((148, y - 12, 172, y + 12), fill=(150, 150, 150))
    if edit:
        draw.rectangle((300, 300, 320, 320), fill="red")
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()


@pytest.mark.parametrize("outcome", ["stable", "moving", "page_change", "only_one"])
def test_measurement_requires_independent_quiet_return(tmp_path, monkeypatch, outcome):
    reference = scene()
    outward = [(.15, scene(520)), (.30, scene(521)), (.45, scene(522))]
    first = scene(615 if outcome == "stable" else 575)
    returned = [(.15, first)]
    if outcome != "only_one":
        later = first if outcome == "stable" else scene(630, edit=outcome == "page_change")
        returned += [(.40, scene(600) if outcome == "moving" else later), (.65, later)]
    result, recorded = replay_probe(tmp_path, monkeypatch, scene(615), reference, outward, returned)
    if outcome == "stable":
        assert result == pytest.approx((.32, 615 / 1080), abs=.003)
        assert len(recorded) == 1
        assert recorded[0][1].frame == first
    else:
        assert result is None
        assert recorded == []
