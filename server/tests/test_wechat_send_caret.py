"""Real-device regressions for verified-field green caret blinking during point and Send click-observed."""

import asyncio
import io
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_host_observed_click import evidence, no_effect, setup

from hidagent import cli, screen_compare
from hidagent.app import ActionStep, ControlAction, HostClickBody
from hidagent.client import IPhoneClient
from hidagent.control import VERIFIED_CARET_SCOPE_TTL, Controls
from hidagent.screen import FrameSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "wechat_send_caret"
FIELD = (0.128, 0.526, 0.764, 0.578)
SEND_TARGET = (0.754, 0.849, 0.998, 0.905)
SEND_POINTER = (0.882021505376344, 0.8611575733439533)
OUTSIDE_FIELD = (0.128, 0.20, 0.764, 0.30)


def load(name: str) -> bytes:
    return (FIXTURES / f"{name}.jpg").read_bytes()


def edited(name: str, draw) -> bytes:
    with Image.open(io.BytesIO(load(name))) as src:
        image = src.convert("RGB")
    draw(ImageDraw.Draw(image))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=92)
    return output.getvalue()


def blink_history(now: float | None = None):
    names = (
        "pointer_on",
        "pointer_outward_off",
        "pointer_outward_mid",
        "pointer_outward_on",
        "pointer_off",
        "pointer_return_settled_on",
    )
    base = (now - 3.0) if now is not None else 0.0
    return tuple(
        SimpleNamespace(frame=load(name), arrived=i * 0.45, updated=base + i * 0.45, generation=1)
        for i, name in enumerate(names)
    )


def seed_history(screen) -> None:
    now_mono = time.monotonic()
    now_wall = time.time()
    names = (
        "pointer_on",
        "pointer_outward_off",
        "pointer_outward_mid",
        "pointer_outward_on",
        "pointer_off",
        "pointer_return_settled_on",
    )
    screen._recent_frames = deque(
        (
            FrameSnapshot(
                frame=load(name),
                board_ts=None,
                arrived=now_mono - 2.6 + i * 0.45,
                updated=now_wall - 2.6 + i * 0.45,
                generation=screen.generation,
                observed_live=True,
            )
            for i, name in enumerate(names)
        ),
        maxlen=32,
    )


def make_host_click_request(screen_id: str, **overrides) -> HostClickBody:
    payload = {
        "screen_id": screen_id,
        "pointer_x": SEND_POINTER[0],
        "pointer_y": SEND_POINTER[1],
        "target_box": SEND_TARGET,
        "target_label": "发送",
        **overrides,
    }
    return HostClickBody(**payload)


@pytest.mark.parametrize("before_name,after_name", [
    ("send_forclick_mid", "send_clicked_on"),
    ("send_clicked_on", "send_forclick_mid"),
    ("send_point_on", "send_forclick_mid"),
    ("send_forclick_mid", "send_point_on"),
    ("admission_mid", "admission_on"),
    ("admission_on", "admission_mid"),
])
def test_real_send_green_caret_blink_passes_symmetrically_only_with_verified_scope(before_name, after_name):
    before, after = load(before_name), load(after_name)
    history = blink_history()
    assert screen_compare.observation_changed(before, after, caret_frames=history)
    assert screen_compare.observation_changed(before, after, caret_frames=history, caret_scope=OUTSIDE_FIELD)
    assert not screen_compare.observation_changed(before, after, caret_frames=history, caret_scope=FIELD)


@pytest.mark.parametrize("tamper", [
    lambda d: d.rectangle((180, 70, 320, 95), fill=(200, 200, 200)),  # conversation title altered
    lambda d: d.line((86, 584, 86, 604), fill=(30, 30, 30), width=2),   # glyph stroke beside caret
    lambda d: d.rectangle((95, 515, 220, 540), fill=(30, 30, 30)),    # draft text altered
    lambda d: d.rounded_rectangle((140, 300, 400, 380), radius=12, fill=(149, 236, 105)),  # new chat bubble
    lambda d: d.rectangle((435, 922, 447, 938), fill=(10, 128, 250)),   # gray pointer missing/altered on Send
])
def test_real_send_caret_scope_never_hides_tamper(tamper):
    before = load("send_forclick_mid")
    after = edited("send_clicked_on", tamper)
    assert screen_compare.observation_changed(before, after, caret_frames=blink_history(), caret_scope=FIELD)


def test_real_send_green_caret_still_requires_complete_blink_history():
    before, after = load("send_forclick_mid"), load("send_clicked_on")
    one_way = tuple(
        SimpleNamespace(frame=load(name), arrived=i * 0.45, generation=1)
        for i, name in enumerate(("pointer_on", "pointer_outward_on", "pointer_outward_off"))
    )
    assert screen_compare.observation_changed(before, after, caret_frames=one_way, caret_scope=FIELD)


def test_real_pointer_probe_overlay_pair_handles_green_caret_blink_with_scope():
    history = blink_history()
    ref = load("pointer_on")
    before = load("pointer_outward_mid")
    after = load("pointer_return_settled_on")
    args = (ref, before, after, (0.876, 0.877), -0.04878048780487805, (0.17, 0.055))
    kwargs = {"reference_pointer": None, "report_reference": True, "axis": 0, "caret_frames": history}
    assert screen_compare.cursor_overlay_pair(*args, **kwargs) is None
    paired = screen_compare.cursor_overlay_pair(*args, **kwargs, caret_scope=FIELD)
    assert paired is not None
    pair, _ = paired
    assert SEND_TARGET[0] < pair[1][0] < SEND_TARGET[2]
    assert SEND_TARGET[1] < pair[1][1] < SEND_TARGET[3]


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("before_name,after_name", [
    ("send_forclick_mid", "send_clicked_on"),
    ("send_point_on", "send_forclick_mid"),
])
def test_click_observed_real_send_caret_blink_requires_caret_scope(tmp_path, qualified, before_name, after_name):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        try:
            screen._on_jpeg(load(before_name))
            seed_history(screen)
            observed = await phone.observe_host_pointer() if qualified else None
            req = evidence(phone, screen)
            req.update(pointer_xy=SEND_POINTER, target_box=SEND_TARGET)
            screen._on_jpeg(load(after_name))
            seed_history(screen)
            with pytest.raises(RuntimeError, match="变化"):
                await phone.click_observed(**req, host_observation=observed)
            assert link.written == []

            screen._on_jpeg(load(before_name))
            seed_history(screen)
            observed2 = await phone.observe_host_pointer() if qualified else None
            req2 = evidence(phone, screen)
            req2.update(pointer_xy=SEND_POINTER, target_box=SEND_TARGET)
            screen._on_jpeg(load(after_name))
            seed_history(screen)
            result = await phone.click_observed(**req2, host_observation=observed2, caret_scope=FIELD)
            assert result["clicked"] is True
            assert link.written == [b"1 click\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tamper", [
    lambda d: d.rectangle((180, 70, 320, 95), fill=(200, 200, 200)),  # title changed
    lambda d: d.rectangle((95, 515, 220, 540), fill=(30, 30, 30)),    # draft changed
    lambda d: d.rectangle((435, 922, 447, 938), fill=(10, 128, 250)),   # Send pointer hidden
])
def test_click_observed_with_caret_scope_still_aborts_on_real_tamper(tmp_path, tamper):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        try:
            screen._on_jpeg(load("send_forclick_mid"))
            seed_history(screen)
            observed = await phone.observe_host_pointer()
            req = evidence(phone, screen)
            req.update(pointer_xy=SEND_POINTER, target_box=SEND_TARGET)
            screen._on_jpeg(edited("send_clicked_on", tamper))
            seed_history(screen)
            with pytest.raises(RuntimeError, match="变化"):
                await phone.click_observed(**req, host_observation=observed, caret_scope=FIELD)
            assert link.written == []
        finally:
            device.close()

    asyncio.run(scenario())


def test_controls_point_and_click_observed_with_explicit_and_inherited_caret_scope(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        controls = Controls(phone, screen)
        phone._tap_effect = no_effect

        async def fake_point_verified(x, y, *, target_box=None, rehome=False, caret_scope=None):
            assert caret_scope == FIELD
            assert phone._pointer_confirmed or True
            phone._pointer_confirmed = True
            return SEND_POINTER

        monkeypatch.setattr(phone, "point_verified", fake_point_verified)
        try:
            # 1. Without explicit or remembered scope, admission_mid -> admission_on rejects point (HTTP 409)
            screen._on_jpeg(load("admission_mid"))
            seed_history(screen)
            obs0 = controls.observation()
            screen._on_jpeg(load("admission_on"))
            seed_history(screen)
            with pytest.raises(ValueError, match="screen changed since observation"):
                await controls.perform(ControlAction(
                    action="point", screen_id=obs0["screen_id"], x=0.876, y=0.877, target_box=SEND_TARGET))

            # 2. Explicit caret_scope on point passes admission on real green-caret blink frames
            screen._on_jpeg(load("admission_mid"))
            seed_history(screen)
            obs1 = controls.observation()
            screen._on_jpeg(load("admission_on"))
            seed_history(screen)
            res_point = await controls.perform(ControlAction(
                action="point", screen_id=obs1["screen_id"], x=0.876, y=0.877,
                target_box=SEND_TARGET, caret_scope=FIELD))
            assert res_point["command_acknowledged"] is True

            # 3. Post-paste remembered scope carries across point -> click_observed, then clears after click_observed
            generation = screen.generation
            controls.remember_verified_caret_scope(
                FIELD, device=device, links=device.links, epoch=device.abort_epoch, generation=generation)

            screen._on_jpeg(load("admission_mid"))
            seed_history(screen)
            obs2 = controls.observation()
            screen._on_jpeg(load("admission_on"))
            seed_history(screen)
            res_point2 = await controls.perform(ControlAction(
                action="point", screen_id=obs2["screen_id"], x=0.876, y=0.877, target_box=SEND_TARGET))
            assert res_point2["command_acknowledged"] is True
            assert controls._verified_caret_scope is not None

            screen._on_jpeg(load("send_forclick_mid"))
            seed_history(screen)
            obs3 = await controls.observe_for_click()
            screen._on_jpeg(load("send_clicked_on"))
            seed_history(screen)
            res_click = await controls.click_observed(make_host_click_request(obs3["screen_id"]))
            assert res_click["command_acknowledged"] is True
            assert link.written == [b"1 click\n"]
            assert controls._verified_caret_scope is None

            # 4. Subsequent click_observed without explicit scope no longer inherits the cleared post-paste scope
            clock.advance(1.0)
            screen._on_jpeg(load("send_forclick_mid"))
            seed_history(screen)
            obs4 = await controls.observe_for_click()
            screen._on_jpeg(load("send_clicked_on"))
            seed_history(screen)
            res_rejected = await controls.click_observed(make_host_click_request(obs4["screen_id"]))
            assert res_rejected["command_acknowledged"] is False
            assert "变化" in (res_rejected["error"] or "")
            assert len(link.written) == 1
        finally:
            device.close()

    asyncio.run(scenario())


def test_remembered_caret_scope_invalidates_on_ttl_or_session_change(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)

    phone, device, screen, _ = setup(tmp_path)
    controls = Controls(phone, screen)
    try:
        controls.remember_verified_caret_scope(
            FIELD, device=device, links=device.links, epoch=device.abort_epoch, generation=screen.generation)
        assert controls._resolve_caret_scope(None, inherit=True) == FIELD

        clock.advance(VERIFIED_CARET_SCOPE_TTL + 1.0)
        assert controls._resolve_caret_scope(None, inherit=True) is None
        assert controls._verified_caret_scope is None

        controls.remember_verified_caret_scope(
            FIELD, device=device, links=device.links, epoch=device.abort_epoch, generation=screen.generation)
        screen.generation += 1
        screen._on_jpeg(load("admission_on"))
        assert controls._resolve_caret_scope(None, inherit=True) is None

        controls.remember_verified_caret_scope(
            FIELD, device=device, links=device.links, epoch=device.abort_epoch, generation=screen.generation)
        device._abort_epoch += 1
        assert controls._resolve_caret_scope(None, inherit=True) is None
    finally:
        device.close()


def test_api_models_cli_and_client_caret_scope_contract():
    body = HostClickBody(
        screen_id="s1", pointer_x=0.88, pointer_y=0.86,
        target_box=SEND_TARGET, target_label="发送", caret_scope=FIELD)
    assert body.caret_scope == FIELD

    step = ActionStep(action="point", x=0.88, y=0.86, target_box=SEND_TARGET, caret_scope=FIELD)
    assert step.caret_scope == FIELD

    args_click = cli.parser().parse_args([
        "click-observed", "0.88", "0.86", "--screen-id", "s1",
        "--target-box", "0.754", "0.849", "0.998", "0.905",
        "--target-label", "发送",
        "--caret-scope", "0.128", "0.526", "0.764", "0.578",
    ])
    assert args_click.caret_scope == [0.128, 0.526, 0.764, 0.578]

    args_point = cli.parser().parse_args([
        "point", "0.88", "0.86", "--screen-id", "s1",
        "--target-box", "0.754", "0.849", "0.998", "0.905",
        "--caret-scope", "0.128", "0.526", "0.764", "0.578",
    ])
    assert cli._action_fields(args_point)["caret_scope"] == [0.128, 0.526, 0.764, 0.578]

    bad_args = cli.parser().parse_args([
        "tap", "0.88", "0.86", "--caret-scope", "0.128", "0.526", "0.764", "0.578",
    ])
    with pytest.raises(ValueError, match="require --screen-id"):
        cli._action_fields(bad_args)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["path"] = request.url.path
        captured["json"] = _json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"command_acknowledged": True})

    with IPhoneClient("http://testserver", transport=httpx.MockTransport(handler)) as client:
        res = client.click_observed("s1", 0.88, 0.86, SEND_TARGET, target_label="发送", caret_scope=FIELD)
    assert res["command_acknowledged"] is True
    assert captured["path"] == "/api/control/host-click"
    assert captured["json"]["caret_scope"] == list(FIELD)


def _with_green_caret(base_name: str, color: tuple[int, int, int], *, x0: int = 250, width: int = 2) -> bytes:
    def paint(draw: ImageDraw.ImageDraw) -> None:
        draw.rectangle((246, 578, 262, 614), fill=(255, 255, 255))
        draw.rectangle((x0, 581, x0 + width - 1, 610), fill=color)

    return edited(base_name, paint)


def test_green_caret_h264_blue_chroma_jitter_passes_in_blink_and_steady_on_modes():
    off = _with_green_caret("pointer_outward_off", (255, 255, 255))
    deep_a = _with_green_caret("pointer_outward_off", (0, 195, 58))
    jitter_b = _with_green_caret("pointer_outward_off", (8, 216, 138))
    deep_c = _with_green_caret("pointer_outward_off", (0, 194, 57))

    for seq in (
        (jitter_b, off, deep_c),
        (off, jitter_b, off, deep_c),
        (deep_c, off, jitter_b),
        (deep_a, jitter_b, deep_c),
    ):
        hist = tuple(
            SimpleNamespace(frame=frame, arrived=i * 0.45, generation=1)
            for i, frame in enumerate(seq)
        )
        assert not screen_compare.observation_changed(jitter_b, deep_c, caret_frames=hist, caret_scope=FIELD)
        assert screen_compare.observation_changed(jitter_b, deep_c, caret_frames=hist)
        assert screen_compare.observation_changed(jitter_b, deep_c, caret_frames=hist, caret_scope=OUTSIDE_FIELD)


@pytest.mark.parametrize("tamper", [
    "dark_on_caret",
    "dark_beside_caret",
    "red_stroke",
    "blue_stroke",
    "shifted_column",
    "mid_to_on_without_off",
])
def test_green_caret_chroma_jitter_never_hides_non_green_or_position_tamper(tamper):
    deep_a = _with_green_caret("pointer_outward_off", (0, 195, 58))
    jitter_b = _with_green_caret("pointer_outward_off", (8, 216, 138))
    before = jitter_b

    if tamper == "dark_on_caret":
        after = edited("pointer_outward_off", lambda d: (
            d.rectangle((246, 578, 262, 614), fill=(255, 255, 255)),
            d.rectangle((250, 581, 251, 610), fill=(0, 195, 58)),
            d.line((250, 586, 250, 598), fill=(30, 30, 30), width=1),
        ))
    elif tamper == "dark_beside_caret":
        after = edited("pointer_outward_off", lambda d: (
            d.rectangle((246, 578, 262, 614), fill=(255, 255, 255)),
            d.rectangle((250, 581, 251, 610), fill=(0, 195, 58)),
            d.line((257, 586, 257, 600), fill=(30, 30, 30), width=2),
        ))
    elif tamper == "red_stroke":
        after = _with_green_caret("pointer_outward_off", (215, 40, 40))
    elif tamper == "blue_stroke":
        after = _with_green_caret("pointer_outward_off", (15, 110, 240))
    elif tamper == "shifted_column":
        after = _with_green_caret("pointer_outward_off", (0, 195, 58), x0=258)
    else:
        # Fading mid-phase without an off phase in history must still fail.
        before = _with_green_caret("pointer_outward_off", (155, 222, 187))
        after = _with_green_caret("pointer_outward_off", (8, 216, 138))
        deep_a = _with_green_caret("pointer_outward_off", (160, 224, 190))

    hist = tuple(
        SimpleNamespace(frame=frame, arrived=i * 0.45, generation=1)
        for i, frame in enumerate((deep_a, before, after))
    )
    assert screen_compare.observation_changed(before, after, caret_frames=hist, caret_scope=FIELD)
