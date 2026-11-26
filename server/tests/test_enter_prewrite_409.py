"""Tests for pre-write HTTP 409 propagation, 4s frame thinning, and H.264 green caret residual off-phases."""

import asyncio
import io
import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_control_input_context import Link
from test_controls import jpeg

from hidagent import app as app_module
from hidagent import cli, feedback, freshness, screen_compare
from hidagent.client import ClientHTTPError, IPhoneClient
from hidagent.control import Controls
from hidagent.device import Device
from hidagent.guard import Guard
from hidagent.phone import Phone
from hidagent.screen import FrameSnapshot, Screen

CARET_FIELD = (0.13, 0.43, 0.76, 0.58)
CARET_X0, CARET_Y0, CARET_X1, CARET_Y1 = 228, 535, 231, 565


def _field_jpeg(caret_color: tuple[int, int, int] | None, *, tamper=None) -> bytes:
    """Create a 500x1080 WeChat-like light input field frame with optional green caret/tamper."""
    img = Image.new("RGB", (500, 1080), (237, 237, 237))
    draw = ImageDraw.Draw(img)
    # Chat header bar
    draw.rectangle((0, 0, 500, 110), fill=(237, 237, 237))
    draw.text((160, 68), "Group Chat (198)", fill=(25, 25, 25))
    # White input box inside CARET_FIELD (65..380, 464..626)
    draw.rounded_rectangle((65, 464, 382, 626), radius=10, fill=(254, 255, 255), outline=(225, 225, 225))
    if caret_color is not None:
        draw.rectangle((CARET_X0, CARET_Y0, CARET_X1 - 1, CARET_Y1 - 1), fill=caret_color)
    if tamper is not None:
        tamper(draw)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _setup_api_rig(tmp_path, monkeypatch):
    clock = ManualClock(wall=datetime(2026, 11, 26, 17, 0).timestamp())
    clock.install(monkeypatch)
    monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)

    device, link, screen = Device(), Link(), Screen()
    device.attach(link)
    clock.advance(0.1)
    screen._on_jpeg(jpeg("white"))
    phone = Phone(device, tmp_path / "missing-calibration.json", screen)
    phone._clock, phone._clock_link = 0, device.links
    controls = Controls(phone, screen)
    guard = Guard(clock=clock.time, path=tmp_path / "guard.json")
    device.gate = app_module._hid_gate

    async def no_effect(*_args, **_kwargs):
        return False

    monkeypatch.setattr(screen, "settled", no_effect)
    monkeypatch.setattr(feedback, "wait_after_action", no_effect)
    for name, value in {
        "device": device,
        "phone": phone,
        "screen": screen,
        "controls": controls,
        "guard": guard,
        "agent": SimpleNamespace(running=False),
        "manual_lock": asyncio.Lock(),
    }.items():
        monkeypatch.setattr(app_module, name, value)
    return SimpleNamespace(clock=clock, device=device, link=link, screen=screen,
                           phone=phone, controls=controls, guard=guard)


def _make_sync_transport(loop):
    async def forward(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                     base_url="http://127.0.0.1") as client:
            response = await client.request(request.method, request.url.raw_path.decode(),
                                            content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    return httpx.MockTransport(
        lambda request: asyncio.run_coroutine_threadsafe(forward(request), loop).result(timeout=10)
    )


def _forward_cli(loop, monkeypatch, capsys):
    transport = _make_sync_transport(loop)
    monkeypatch.setattr(cli, "IPhoneClient", lambda url: IPhoneClient(url or "http://test", transport=transport))

    def run_cli(*argv):
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, json.loads(captured.err if captured.err.strip() else captured.out)

    return run_cli, transport


def test_prewrite_409_propagates_false_across_api_client_and_cli(tmp_path, monkeypatch, capsys):
    rig = _setup_api_rig(tmp_path, monkeypatch)

    async def scenario():
        loop = asyncio.get_running_loop()
        run_cli, transport = _forward_cli(loop, monkeypatch, capsys)

        # 1. FastAPI /api/control/action pre-write 409 includes action_may_have_executed=False.
        obs_id = rig.controls.observation()["screen_id"]
        rig.clock.advance(0.1)
        rig.screen._on_jpeg(jpeg("black"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                     base_url="http://127.0.0.1") as client:
            resp = await client.post("/api/control/action", json={
                "action": "key", "text": "enter", "screen_id": obs_id,
            })
        assert resp.status_code == 409
        body = resp.json()
        assert body["action_may_have_executed"] is False
        assert "screen changed since" in body["detail"].lower()
        assert rig.link.written == []

        # 2. enter_intent pre-check 409 also preserves action_may_have_executed=False.
        obs_id_intent = rig.controls.observation()["screen_id"]
        rig.clock.advance(0.1)
        rig.screen._on_jpeg(jpeg("white"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                     base_url="http://127.0.0.1") as client:
            resp_intent = await client.post("/api/control/action", json={
                "action": "key", "text": "enter", "screen_id": obs_id_intent,
                "enter_intent": "navigation",
            })
        assert resp_intent.status_code == 409
        assert resp_intent.json()["action_may_have_executed"] is False
        assert rig.link.written == []

        # 3. IPhoneClient preserves status_code + payload in ClientHTTPError (subclass of RuntimeError).
        obs_id_client = rig.controls.observation()["screen_id"]
        rig.clock.advance(0.1)
        rig.screen._on_jpeg(jpeg("black"))

        def sync_post():
            with IPhoneClient("http://test", transport=transport) as client:
                client.control("key", obs_id_client, text="enter")

        with pytest.raises(ClientHTTPError) as exc_info:
            await asyncio.to_thread(sync_post)
        assert isinstance(exc_info.value, RuntimeError)
        assert exc_info.value.status_code == 409
        assert exc_info.value.payload.get("action_may_have_executed") is False

        # 4. CLI emits structured status_code and action_may_have_executed=False on stderr.
        obs_id_cli = rig.controls.observation()["screen_id"]
        rig.clock.advance(0.1)
        rig.screen._on_jpeg(jpeg("white"))
        payload = json.dumps({"action": "key", "text": "enter", "screen_id": obs_id_cli})
        code, err_payload = await asyncio.to_thread(run_cli, "action", payload)
        assert code == 2
        assert err_payload["status_code"] == 409
        assert err_payload["action_may_have_executed"] is False
        assert "screen changed since" in err_payload["error"].lower()
        assert rig.link.written == []

    try:
        asyncio.run(scenario())
    finally:
        rig.device.close()


def test_postwrite_409_after_wake_nudge_never_claims_false(tmp_path, monkeypatch, capsys):
    rig = _setup_api_rig(tmp_path, monkeypatch)

    async def scenario():
        loop = asyncio.get_running_loop()
        run_cli, _transport = _forward_cli(loop, monkeypatch, capsys)

        obs_id = rig.controls.observation()["screen_id"]
        # Age current frame past OBSERVATION_ENTRY_MAX_AGE so Controls.perform invokes ensure_observation.
        rig.clock.advance(2.5)

        async def write_then_mutate(*, before_refresh=None):
            if before_refresh is not None:
                before_refresh()
            # Consequential HID write inside admission sets admission.written=True, followed by layout change.
            await rig.device.send("click")
            rig.clock.advance(0.1)
            rig.screen._on_jpeg(jpeg("black"))

        monkeypatch.setattr(rig.phone, "ensure_observation", write_then_mutate)
        payload = json.dumps({"action": "key", "text": "enter", "screen_id": obs_id})
        code, err_payload = await asyncio.to_thread(run_cli, "action", payload)
        assert code == 2
        assert err_payload["status_code"] == 409
        assert "action_may_have_executed" not in err_payload
        assert len(rig.link.written) == 1

    try:
        asyncio.run(scenario())
    finally:
        rig.device.close()


def test_host_click_prewrite_409_propagates_action_may_have_executed_false(tmp_path, monkeypatch):
    rig = _setup_api_rig(tmp_path, monkeypatch)

    async def scenario():
        obs_id = rig.controls.observation()["screen_id"]
        rig.clock.advance(0.1)
        rig.screen._on_jpeg(jpeg("black"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                     base_url="http://127.0.0.1") as client:
            resp = await client.post("/api/control/host-click", json={
                "screen_id": obs_id,
                "pointer_x": 0.5,
                "pointer_y": 0.4,
                "target_box": [0.4, 0.3, 0.6, 0.5],
                "target_label": "发送",
            })
        assert resp.status_code == 409
        assert resp.json()["action_may_have_executed"] is False
        assert rig.link.written == []

    try:
        asyncio.run(scenario())
    finally:
        rig.device.close()


def test_recent_frame_history_preserves_4s_span_under_high_fps(monkeypatch):
    clock = ManualClock(monotonic=1000.0)
    clock.install(monkeypatch)
    screen = Screen()
    screen.generation = 1

    pushed = []
    for i in range(120):
        t = clock.monotonic()
        snap = FrameSnapshot(f"frame-{i}".encode(), None, t, generation=1, observed_live=True, updated=clock.time())
        screen._remember_snapshot(snap)
        pushed.append(snap)
        clock.advance(1 / 30)

    recent = screen.recent_frames
    assert len(recent) == 32
    assert recent[-1].arrived - recent[0].arrived >= 3.5
    assert [snap.frame for snap in recent[-6:]] == [snap.frame for snap in pushed[-6:]]


def test_green_caret_residual_off_phase_and_adversarial_tampers():
    solid_on = _field_jpeg((42, 166, 82))
    mid_fade = _field_jpeg((147, 225, 183))
    residual_off = _field_jpeg((210, 238, 224))

    full_cycle = tuple(
        SimpleNamespace(frame=frame, arrived=100.0 + idx * 0.45, generation=1)
        for idx, frame in enumerate((residual_off, solid_on, residual_off, solid_on, mid_fade))
    )

    assert not screen_compare.observation_changed(
        mid_fade, residual_off, caret_frames=full_cycle, caret_scope=CARET_FIELD)
    assert not screen_compare.observation_changed(
        residual_off, mid_fade, caret_frames=full_cycle, caret_scope=CARET_FIELD)

    # Missing or wrong caret_scope must reject.
    assert screen_compare.observation_changed(mid_fade, residual_off, caret_frames=full_cycle)
    assert screen_compare.observation_changed(
        mid_fade, residual_off, caret_frames=full_cycle, caret_scope=(0.13, 0.15, 0.76, 0.30))

    # One-way sequence (no reversible 3-state blink) must reject.
    one_way = tuple(
        SimpleNamespace(frame=frame, arrived=100.0 + idx * 0.45, generation=1)
        for idx, frame in enumerate((solid_on, mid_fade, residual_off))
    )
    assert screen_compare.observation_changed(
        mid_fade, residual_off, caret_frames=one_way, caret_scope=CARET_FIELD)

    # Weak mid-fade pretending to be ON paired with residual_off must reject.
    weak_cycle = tuple(
        SimpleNamespace(frame=frame, arrived=100.0 + idx * 0.45, generation=1)
        for idx, frame in enumerate((residual_off, mid_fade, residual_off, mid_fade))
    )
    assert screen_compare.observation_changed(
        mid_fade, residual_off, caret_frames=weak_cycle, caret_scope=CARET_FIELD)

    # Adversarial edits on field and screen must always be caught.
    for edit in (
        lambda d: d.rectangle((228, 548, 230, 552), fill=(30, 30, 30)),       # black dot in caret column
        lambda d: d.rectangle((228, 545, 230, 553), fill=(9, 199, 101)),      # short green tick in column
        lambda d: d.rectangle((228, 535, 230, 564), fill=(0, 120, 255)),      # blue line in caret column
        lambda d: d.line((236, 536, 236, 560), fill=(30, 30, 30), width=2),   # adjacent glyph stroke
        lambda d: d.rectangle((160, 68, 340, 95), fill=(180, 180, 180)),      # chat header changed
    ):
        tampered = _field_jpeg((210, 238, 224), tamper=edit)
        assert screen_compare.observation_changed(
            mid_fade, tampered, caret_frames=full_cycle, caret_scope=CARET_FIELD)
