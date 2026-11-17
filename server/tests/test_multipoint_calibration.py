"""Tests for backward-compatible 6-parameter affine multi-point calibration."""

import asyncio
import io
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from hidagent import phone as phone_module
from hidagent.control import Controls
from hidagent.phone import BLE_PACE, Phone, PointerAffineFit, fit_affine_samples
from hidagent.text_target import PointerLandingError, TargetChangedError

SUGGESTION_FIXTURES = Path(__file__).parent / "fixtures" / "suggestion_bar"


def _canvas_frame(pointer_xy=None, *, size=(200, 430), bg=(238, 242, 246), square=None):
    image = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(image)
    if square is not None:
        x0, y0, x1, y1 = square
        draw.rectangle((x0 * size[0], y0 * size[1], x1 * size[0], y1 * size[1]), fill=(207, 224, 255))
    if pointer_xy is not None:
        px, py = pointer_xy[0] * size[0], pointer_xy[1] * size[1]
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=(150, 150, 150))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


class _FakeAffineMirror:
    """Simulated BLE HID + AirPlay mirror with skewed linear pointer response."""

    live = True
    generation = 1
    links = 1
    abort_epoch = 0
    board_status = {"hid": {"path": "ble", "pace": BLE_PACE}}
    pace_version = BLE_PACE
    port_name = "/dev/cu.usbserial-FAKE"
    recent_frames = ()

    def __init__(self, *, a_xx=0.0038, a_xy=0.00018, a_yx=-0.00012, a_yy=0.00185, x0=0.095, y0=0.015, visible=True):
        self.true_fit = PointerAffineFit(a_xx, a_xy, a_yx, a_yy, x0, y0)
        self.u = 0
        self.v = 0
        self.visible = visible
        self.commands = []
        self.pending = []
        self.square = None
        self.frame = _canvas_frame()
        self.updated = time.time()
        self.frame_ts = None
        self.clock_ms = 10000

    @property
    def aspect(self):
        return 200.0 / 430.0

    def pointer_norm(self):
        return self.true_fit.forward(self.u, self.v)

    def _publish(self, *, immediate=True):
        ptr = self.pointer_norm() if self.visible else None
        new_frame = _canvas_frame(ptr, square=self.square)
        if immediate:
            self.frame = new_frame
            self.updated = time.time() + 0.005
            self.clock_ms += 40
        else:
            self.pending.append(new_frame)

    async def query(self, command):
        if command == "pace":
            return f"ok {BLE_PACE}"
        if command == "status":
            return json.dumps({"up": self.clock_ms, "hid": self.board_status["hid"]})
        return "ok"

    async def send(self, command):
        self.commands.append(command)
        parts = command.split()
        if parts[0] == "home":
            self.u = self.v = 0
        elif parts[0] == "move":
            self.u = max(0, self.u + int(parts[1]))
            self.v = max(0, self.v + int(parts[2]))
        self._publish(immediate=False)

    async def send_guarded(self, command, before_write, *, write_lock=None):
        before_write()
        await self.send(command)

    async def fresh(self, since, timeout):
        if self.pending:
            await asyncio.sleep(min(0.002, timeout))
            self.frame = self.pending.pop(0)
            self.updated = max(time.time(), since + 0.005)
            self.clock_ms += 40
            return self.frame
        if self.updated > since:
            return self.frame
        await asyncio.sleep(min(0.01, timeout))
        return self.frame if self.updated > since else None

    async def settled(self, *, quiet=0.2, least=0.3, most=1.5, transport=False):
        if self.pending:
            self.frame = self.pending.pop(-1)
            self.pending.clear()
            self.updated = time.time()
        return self.frame


def test_legacy_two_point_calibration_file_remains_compatible(tmp_path):
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(
        json.dumps(
            {
                "x0": 0.10,
                "y0": 0.0,
                "w": 240,
                "h": 520,
                "pace": BLE_PACE,
                "seen": {"x": [[0.2, 0.2], [0.8, 0.8]], "y": [[0.2, 0.2], [0.8, 0.8]]},
            }
        ),
        encoding="utf-8",
    )
    mirror = _FakeAffineMirror()
    phone = Phone(mirror, cal_file, mirror)

    assert phone.calibrated is True
    assert phone._affine is None
    assert phone._target_units(0.5, 0.5) == (phone._dx(0.5), phone._dy(0.5)) == (96, 260)
    assert phone._delta_units(0.1, -0.1) == (24, -52)

    phone._save()
    saved = json.loads(cal_file.read_text(encoding="utf-8"))
    assert "affine" not in saved
    assert saved["w"] == 240 and saved["h"] == 520


def test_corrupt_or_singular_affine_in_file_falls_back_to_two_point(tmp_path):
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(
        json.dumps(
            {
                "x0": 0.10,
                "y0": 0.0,
                "w": 250,
                "h": 550,
                "pace": BLE_PACE,
                "affine": {"a_xx": 0.0, "a_xy": 0.0, "a_yx": 0.0, "a_yy": 0.002, "x0": 0.1, "y0": 0.0},
            }
        ),
        encoding="utf-8",
    )
    mirror = _FakeAffineMirror()
    phone = Phone(mirror, cal_file, mirror)

    assert phone.calibrated is True
    assert phone._affine is None
    assert phone._target_units(0.5, 0.5) == (100, 275)


def test_fit_affine_samples_recovers_skewed_plane_and_resists_outlier():
    true_fit = PointerAffineFit(0.0038, 0.00018, -0.00012, 0.00185, 0.095, 0.008)
    grid_uv = [(u, v) for v in (100, 260, 410) for u in (30, 110, 190)]
    clean = [(float(u), float(v), *true_fit.forward(u, v)) for u, v in grid_uv]

    fit = fit_affine_samples(clean)
    assert fit is not None
    assert abs(fit.a_xx - true_fit.a_xx) < 1e-8
    assert abs(fit.a_xy - true_fit.a_xy) < 1e-8
    assert abs(fit.a_yx - true_fit.a_yx) < 1e-8
    assert abs(fit.a_yy - true_fit.a_yy) < 1e-8
    assert abs(fit.x0 - true_fit.x0) < 1e-7
    assert abs(fit.y0 - true_fit.y0) < 1e-7

    # Round-trip inverse recovers units cleanly.
    ru, rv = fit.inverse(*fit.forward(142.0, 333.0))
    assert math.isclose(ru, 142.0, abs_tol=1e-7) and math.isclose(rv, 333.0, abs_tol=1e-7)

    # Inject a corrupted reading on one interior sample: IRLS downweights the outlier.
    noisy = list(clean)
    u4, v4, x4, y4 = noisy[4]
    noisy[4] = (u4, v4, x4 + 0.075, y4 - 0.065)
    robust = fit_affine_samples(noisy)
    assert robust is not None
    px, py = robust.forward(150.0, 300.0)
    tx, ty = true_fit.forward(150.0, 300.0)
    assert math.hypot(px - tx, py - ty) < 0.01

    # Invalid cases must return None rather than a degenerate matrix.
    assert fit_affine_samples(clean[:5]) is None
    collinear = [(float(u), float(u), 0.1 + 0.003 * u, 0.01 + 0.002 * u) for u in range(10, 70, 10)]
    assert fit_affine_samples(collinear) is None


def test_multipoint_start_calibration_persists_affine_and_falls_back_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_module, "POINTER_WAIT", 1.0)
    mirror = _FakeAffineMirror()
    phone = Phone(mirror, tmp_path / "calibration.json", mirror)
    phone._clock = 0.0
    phone._clock_link = mirror.links

    ok = asyncio.run(phone.start_calibration(multipoint=True))
    assert ok is True
    assert phone.calibrated is True
    assert phone._affine is not None
    assert abs(phone._affine.a_xx - mirror.true_fit.a_xx) < 0.0005
    assert abs(phone._affine.a_xy - mirror.true_fit.a_xy) < 0.0005

    saved = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    assert "affine" in saved and "w" in saved and "h" in saved

    # Reload from disk preserves affine mapping, and pace change clears it cleanly.
    reloaded = Phone(mirror, tmp_path / "calibration.json", mirror)
    assert reloaded._affine == phone._affine
    reloaded.adopt_pace(99)
    assert reloaded._affine is None and reloaded.span is None

    # If the 9-grid visibility drops after coarse 2-point stops, multipoint falls back safely.
    mirror2 = _FakeAffineMirror()
    phone2 = Phone(mirror2, tmp_path / "fallback_cal.json", mirror2)
    phone2._clock = 0.0
    phone2._clock_link = mirror2.links

    async def empty_grid(*args, **kwargs):
        return []

    monkeypatch.setattr(phone2, "_collect_multipoint_samples", empty_grid)
    ok_fb = asyncio.run(phone2.start_calibration(multipoint=True))
    assert ok_fb is True
    assert phone2.calibrated is True
    assert phone2._affine is None


def test_affine_calibration_preserves_safety_and_suggestion_bar_guards(tmp_path):
    mirror = _FakeAffineMirror()
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(
        json.dumps(
            {
                "x0": 0.095,
                "y0": 0.010,
                "w": 263,
                "h": 541,
                "pace": BLE_PACE,
                "affine": mirror.true_fit.to_dict(),
                "seen": {"x": [[0.2, 0.2], [0.8, 0.8]], "y": [[0.2, 0.2], [0.8, 0.8]]},
            }
        ),
        encoding="utf-8",
    )
    phone = Phone(mirror, cal_file, mirror)
    phone._clock = 0.0
    phone._clock_link = mirror.links
    assert phone._affine is not None

    # 1. Stale frame is rejected before any move/click.
    mirror.updated = time.time() - 30.0
    with pytest.raises(RuntimeError):
        asyncio.run(phone.tap_verified(0.5, 0.5, target_box=(0.45, 0.45, 0.55, 0.55), target_frame=mirror.frame))
    assert "click" not in mirror.commands

    # 2. Target changed before click raises TargetChangedError without sending click.
    mirror.square = (0.45, 0.45, 0.55, 0.55)
    mirror._publish()
    original_frame = mirror.frame

    async def hijack_target(x, y, **kwargs):
        phone._pointer_confirmed = True
        mirror.square = None
        mirror.frame = _canvas_frame((x, y), bg=(20, 20, 20))
        mirror.updated = time.time()
        mirror.frame_ts = mirror.updated * 1000
        return x, y

    phone.point_verified = hijack_target
    with pytest.raises(TargetChangedError):
        asyncio.run(phone.tap_verified(0.5, 0.5, target_box=(0.45, 0.45, 0.55, 0.55), target_frame=original_frame))
    assert "click" not in mirror.commands

    # 3. Unconfirmed pointer landing is blocked by PointerLandingError.
    phone2 = Phone(mirror, cal_file, mirror)
    phone2._clock = 0.0
    phone2._clock_link = mirror.links
    mirror.square = (0.45, 0.45, 0.55, 0.55)
    mirror.visible = False
    mirror._publish()

    async def unconfirmed(x, y, **kwargs):
        phone2._pointer_confirmed = False
        return x, y

    phone2.point_verified = unconfirmed
    with pytest.raises(PointerLandingError):
        asyncio.run(phone2.tap_verified(0.5, 0.5, target_box=(0.45, 0.45, 0.55, 0.55), target_frame=mirror.frame))
    assert "click" not in mirror.commands

    # 4. Bare tap landing on keyboard suggestion bar is blocked even under affine fit.
    keyboard = (SUGGESTION_FIXTURES / "keyboard.jpg").read_bytes()
    mirror.frame = keyboard
    mirror.updated = time.time()
    mirror.frame_ts = mirror.updated * 1000
    phone3 = Phone(mirror, cal_file, mirror)
    phone3._clock = 0.0
    phone3._clock_link = mirror.links

    async def drift_into_bar(x, y, **kwargs):
        phone3._pointer_confirmed = True
        return (0.5, 0.60)

    phone3.point_verified = drift_into_bar
    with pytest.raises(PointerLandingError, match="实测落点在键盘建议栏"):
        asyncio.run(phone3.tap_verified(0.5, 0.55))
    assert "click" not in mirror.commands
    # Ensure Controls import cleanly alongside affine Phone.
    controls = Controls(phone3, mirror)
    assert isinstance(SimpleNamespace(controls=controls).controls, Controls)
