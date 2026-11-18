"""CLI protocol tests: exercise parsing and HTTP serialization without a phone."""

import base64
import json

import httpx
import pytest

from hidagent import cli
from hidagent.client import IPhoneClient


@pytest.fixture
def remote(monkeypatch):
    requests = []

    def respond(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.raw_path.decode(), body))
        return httpx.Response(200, json={
            "screen_id": "next-screen", "width": 400, "height": 800,
            "effect_verified": False, "image_base64": base64.b64encode(b"test jpeg").decode(),
        })

    monkeypatch.setattr(cli, "IPhoneClient", lambda url: IPhoneClient(
        url or "http://test", transport=httpx.MockTransport(respond)))
    return requests


def test_observe_saves_image_with_matching_screen_id(remote, tmp_path, capsys):
    output = tmp_path / "observation.jpg"
    assert cli.main(["observe", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert output.read_bytes() == b"test jpeg"
    assert result["screen_id"] == "next-screen"
    assert result["image_path"] == str(output)
    assert "image_base64" not in result
    assert remote == [("GET", "/api/control/observe", None)]


def test_host_click_observation_explicitly_selects_passive_mode(remote, tmp_path, capsys):
    output = tmp_path / "pointer.jpg"
    assert cli.main(["observe", str(output), "--for-click"]) == 0
    assert json.loads(capsys.readouterr().out)["screen_id"] == "next-screen"
    assert output.read_bytes() == b"test jpeg"
    assert remote == [("GET", "/api/control/observe?for_click=true", None)]


def test_guarded_tap_forwards_observed_context_and_returns_unverified_evidence(remote, capsys):
    assert cli.main(["tap", "200", "320", "--screen-id", "seen-1", "--coordinate-space", "pixels",
                     "--target-box", ".4", ".3", ".6", ".5"]) == 0
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [("POST", "/api/control/action", {
        "action": "tap", "screen_id": "seen-1", "x": 200., "y": 320.,
        "coordinate_space": "pixels", "target_box": [.4, .3, .6, .5],
    })]


def test_explicit_observed_click_has_its_own_route_and_preserves_visible_label(remote, capsys, tmp_path):
    output = tmp_path / "after-host-click.jpg"
    assert cli.main(["click-observed", ".5", ".4", "--screen-id", "seen-pointer",
                     "--target-box", ".4", ".3", ".6", ".5", "--target-label", "主屏幕",
                     "--image-output", str(output)]) == 0
    assert output.read_bytes() == b"test jpeg"
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [("POST", "/api/control/host-click", {
        "screen_id": "seen-pointer", "pointer_x": .5, "pointer_y": .4,
        "target_box": [.4, .3, .6, .5], "target_label": "主屏幕",
    })]


def test_observed_hold_forwards_only_explicit_duration(remote, capsys):
    assert cli.main(["click-observed", ".5", ".4", "--screen-id", "seen-pointer",
                     "--target-box", ".4", ".3", ".6", ".5", "--duration", "1.2"]) == 0
    capsys.readouterr()
    assert remote == [("POST", "/api/control/host-click", {
        "screen_id": "seen-pointer", "pointer_x": .5, "pointer_y": .4,
        "target_box": [.4, .3, .6, .5], "target_label": "", "duration": 1.2,
    })]


@pytest.mark.parametrize("arguments", [
    [".5", ".4", "--target-box", ".4", ".3", ".6", ".5"],
    [".5", ".4", "--screen-id", "fresh"],
    [".5", ".4", "--screen-id", "fresh", "--target-box", ".4", ".3", ".6", ".5", "--repeat", "2"],
])
def test_observed_click_never_falls_back_to_legacy_or_repeats(remote, arguments):
    with pytest.raises(SystemExit) as exc:
        cli.main(["click-observed", *arguments])
    assert exc.value.code == 2 and remote == []


@pytest.mark.parametrize("argv,path,body", [
    (["find-text", "设置", "--screen-id", "seen-1", "--exact", "--region", "0", "0", "1", ".2"],
     "/api/control/find_text", {"screen_id": "seen-1", "text": "设置", "nth": 1,
                                "exact": True, "region": [0., 0., 1., .2]}),
    (["tap-text", "设置", "--screen-id", "seen-1", "--nth", "2"],
     "/api/control/action", {"action": "tap_text", "screen_id": "seen-1", "text": "设置",
                             "nth": 2, "exact": False}),
    (["compose-chinese", "你好", "--screen-id", "seen-1", "--input-mode", "pinyin"],
     "/api/control/action", {"action": "compose_chinese", "screen_id": "seen-1", "text": "你好",
                             "input_mode": "pinyin"}),
    (["key", "enter", "--screen-id", "seen-1", "--enter-intent", "navigation"],
     "/api/control/action", {"action": "key", "screen_id": "seen-1", "text": "enter",
                             "repeat": 1, "enter_intent": "navigation"}),
])
def test_guarded_cli_commands_share_mcp_protocol(remote, capsys, argv, path, body):
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["screen_id"] == "next-screen"
    assert remote == [("POST", path, body)]


def test_generic_action_accepts_json_file_without_losing_gesture_fields(remote, tmp_path, capsys):
    payload = {"action": "path", "screen_id": "seen-1", "points": [[.1, .2], [.2, .3], [.3, .2]],
               "coordinate_space": "normalized"}
    source = tmp_path / "action.json"
    source.write_text(json.dumps(payload))
    assert cli.main(["action", f"@{source}"]) == 0
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [("POST", "/api/control/action", payload)]


def test_batch_preserves_stop_result_and_evidence(remote, capsys):
    steps = [{"action": "key", "text": "left", "repeat": 2}]
    assert cli.main(["batch", json.dumps(steps), "--screen-id", "seen-1"]) == 0
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [("POST", "/api/control/batch", {"screen_id": "seen-1", "steps": steps})]


@pytest.mark.parametrize("payload", ["[]", '{"action":"tap"}', '{"screen_id":"seen-1"}',
                                    '{"action":"tap","screen_id":""}', "invalid"])
def test_generic_action_rejects_missing_context_before_request(remote, capsys, payload):
    assert cli.main(["action", payload]) == 2
    assert not remote
    output = capsys.readouterr()
    assert not output.out and "error" in json.loads(output.err)


def test_guarded_only_options_cannot_silently_fall_back_to_legacy_tap(remote, capsys):
    assert cli.main(["tap", ".5", ".4", "--target-box", ".4", ".3", ".6", ".5"]) == 2
    assert not remote
    assert "screen-id" in capsys.readouterr().err


def test_legacy_tap_is_retained(remote):
    assert cli.main(["tap", ".5", ".4"]) == 0
    assert remote == [("POST", "/api/tap", {"x": .5, "y": .4})]


@pytest.mark.parametrize("argv,body", [
    (["path", "[[0.1,0.2],[0.2,0.3]]", "--no-hold"],
     {"action": "path", "points": [[.1, .2], [.2, .3]], "hold": False}),
    (["circle", ".5", ".5", ".2", ".1", "--segments", "24"],
     {"action": "circle", "x": .5, "y": .5, "radius_x": .2, "radius_y": .1,
      "segments": 24, "hold": True}),
    (["long-press", ".5", ".4", "--duration", "1.2"],
     {"action": "long_press", "x": .5, "y": .4, "duration": 1.2}),
    (["double-tap", ".5", ".4"], {"action": "double_tap", "x": .5, "y": .4}),
    (["search-app", "Freeform"], {"action": "open_app", "text": "Freeform"}),
])
def test_new_gesture_commands_use_guarded_actions(remote, capsys, argv, body):
    assert cli.main([*argv, "--screen-id", "seen-1"]) == 0
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [("POST", "/api/control/action", {
        **body, "screen_id": "seen-1", "coordinate_space": "normalized"})]


def test_capabilities_is_read_only(remote):
    assert cli.main(["capabilities"]) == 0
    assert remote == [("GET", "/api/control/capabilities", None)]


def test_explicit_empty_screen_id_cannot_fall_back_to_legacy(remote, capsys):
    assert cli.main(["tap", ".5", ".4", "--screen-id", ""]) == 2
    assert not remote
    assert "screen-id" in capsys.readouterr().err


@pytest.mark.parametrize("argv,stage,body", [
    (["paste-prepare", "你好", "--screen-id", "seen-1", "--focused", "--target-box", ".1", ".2", ".8", ".4"],
     "prepare", {"text": "你好", "screen_id": "seen-1", "focused": True, "target_box": [.1, .2, .8, .4]}),
    (["paste", "tx-1", "--screen-id", "seen-1", "--focused"],
     "paste", {"transaction_id": "tx-1", "screen_id": "seen-1", "focused": True}),
    (["paste-confirm", "tx-1", "--screen-id", "seen-1", "--observed-text", "你好", "--confirmed"],
     "confirm", {"transaction_id": "tx-1", "screen_id": "seen-1", "observed_text": "你好", "confirmed": True}),
    (["paste-status", "tx-1"], "status", {"transaction_id": "tx-1"}),
    (["paste-status", "tx-1", "--no-include-observation"], "status",
     {"transaction_id": "tx-1", "include_observation": False}),
    (["paste-cancel", "tx-1"], "cancel", {"transaction_id": "tx-1"}),
])
def test_staged_paste_commands_keep_separate_evidence_boundaries(remote, capsys, argv, stage, body):
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [("POST", f"/api/control/clipboard/{stage}", body)]


def test_middle_click_home_cli_status_enable_and_revoke(remote, tmp_path, capsys):
    assert cli.main(["middle-click-home", "status"]) == 0
    capsys.readouterr()

    out_img = tmp_path / "home.jpg"
    assert cli.main(["middle-click-home", "enable", "--screen-id", "seen-1", "--image-output", str(out_img)]) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert out_img.read_bytes() == b"test jpeg"
    assert enabled["image_path"] == str(out_img)

    assert cli.main(["middle-click-home", "revoke", "--all"]) == 0
    capsys.readouterr()

    assert remote == [
        ("GET", "/api/control/middle-click-home", None),
        ("POST", "/api/control/middle-click-home/enable", {
            "screen_id": "seen-1", "include_diff": True, "ocr_diff": False,
        }),
        ("POST", "/api/control/middle-click-home/revoke", {"all_devices": True}),
    ]


@pytest.mark.parametrize("argv,expected", [
    (["layouts"], ("GET", "/api/layouts", None)),
    (["skill-list"], ("GET", "/api/skills", None)),
    (["skill-show", "wechat-group-reply"], ("GET", "/api/skills/wechat-group-reply", None)),
    (["skill-check", "--observed-text", "操作频繁", "--skill", "wechat-group-reply"],
     ("POST", "/api/skill/check", {"observed_text": "操作频繁", "skill": "wechat-group-reply"})),
    (["skill-prepare-send", "--skill", "wechat-group-reply"],
     ("POST", "/api/skill/prepare-send", {"skill": "wechat-group-reply"})),
    (["skill-confirm-send", "--bubble-visible", "--observed-text", "已发", "--skill", "wechat-group-reply"],
     ("POST", "/api/skill/confirm-send",
      {"bubble_visible": True, "observed_text": "已发", "skill": "wechat-group-reply"})),
    (["skill-confirm-send", "--no-bubble-visible", "--skill", "wechat-group-reply"],
     ("POST", "/api/skill/confirm-send",
      {"bubble_visible": False, "observed_text": "", "skill": "wechat-group-reply"})),
    (["layout-tap", "wechat-group-reply", "input", "--screen-id", "seen-1"],
     ("POST", "/api/control/layout-tap",
      {"layout": "wechat-group-reply", "point": "input", "screen_id": "seen-1"})),
])
def test_skill_and_layout_cli_subcommands(remote, capsys, argv, expected):
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [expected]


def test_skill_confirm_send_requires_explicit_bubble_visibility_and_layout_tap_saves_image(remote, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["skill-confirm-send", "--skill", "wechat-group-reply"])
    assert exc.value.code == 2 and remote == []
    out = tmp_path / "layout.jpg"
    assert cli.main(["layout-tap", "wechat-group-reply", "send", "--screen-id", "seen-1",
                     "--image-output", str(out)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert out.read_bytes() == b"test jpeg" and payload["image_path"] == str(out)


def test_cli_labeled_send_clicks_enforce_guard_and_emit_zero_clicks_when_refused(tmp_path, monkeypatch, capsys):
    import asyncio
    import time
    from datetime import datetime
    from types import SimpleNamespace

    from test_control_input_context import Link
    from test_controls import jpeg

    from hidagent import app as app_module
    from hidagent import feedback, freshness, ocr
    from hidagent.control import Controls
    from hidagent.device import Device
    from hidagent.guard import Guard
    from hidagent.phone import Phone
    from hidagent.screen import Screen

    async def scenario():
        now = [datetime(2026, 11, 15, 12, 0).timestamp()]
        monkeypatch.setattr(time, "time", lambda: now[0])
        device, link, screen = Device(), Link(), Screen()
        device.attach(link)
        now[0] += freshness.MIRROR_LAG_SECONDS + .1
        screen._on_jpeg(jpeg())
        phone = Phone(device, tmp_path / "missing-calibration.json", screen)
        phone._clock, phone._clock_link = 0, device.links
        controls = Controls(phone, screen)
        guard = Guard(clock=lambda: now[0], path=tmp_path / "guard.json")
        device.gate = app_module._hid_gate

        async def effect(*_args):
            now[0] += freshness.MIRROR_LAG_SECONDS + .1
            screen._on_jpeg(jpeg("black"))
            phone._effect_snapshot = freshness.require_fresh(screen)
            phone._effect_frame = screen.frame
            return True, True

        async def positioned(x, y, **_kwargs):
            phone._pointer_confirmed = True
            return x, y

        async def no_feedback(*_args, **_kwargs):
            return False

        async def find_send(_frame, query, **_kwargs):
            return [ocr.TextBox("发送", (.82, .86, .94, .90), (.88, .88), .99)] if query == "发送" else []

        monkeypatch.setattr(phone, "_tap_effect", effect)
        monkeypatch.setattr(phone, "point_verified", positioned)
        monkeypatch.setattr("hidagent.phone.TAP_GAP", 0)
        monkeypatch.setattr(screen, "settled", no_feedback)
        monkeypatch.setattr(feedback, "wait_after_action", no_feedback)
        monkeypatch.setattr(ocr, "find_matches_async", find_send)
        for name, value in {"device": device, "phone": phone, "screen": screen, "controls": controls,
                            "guard": guard, "agent": SimpleNamespace(running=False),
                            "manual_lock": asyncio.Lock()}.items():
            monkeypatch.setattr(app_module, name, value)

        loop = asyncio.get_running_loop()

        async def forward(request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app),
                                         base_url="http://127.0.0.1") as client:
                response = await client.request(request.method, request.url.raw_path.decode(),
                                                content=request.content, headers=dict(request.headers))
                return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        monkeypatch.setattr(cli, "IPhoneClient", lambda url: IPhoneClient(
            url or "http://test", transport=httpx.MockTransport(
                lambda request: asyncio.run_coroutine_threadsafe(forward(request), loop).result(timeout=10))))

        def run_cli(*argv):
            code = cli.main(list(argv))
            captured = capsys.readouterr()
            return code, json.loads(captured.out) if captured.out.strip() else json.loads(captured.err)

        try:
            # 1. Ordinary unlabeled coordinate click is not classified as Send.
            obs_id = controls.observation()["screen_id"]
            code, plain = await asyncio.to_thread(
                run_cli, "click-observed", ".5", ".4", "--screen-id", obs_id,
                "--target-box", ".4", ".3", ".6", ".5")
            assert code == 0 and plain["command_acknowledged"] is True
            assert len(guard.sends) == 0 and sum(b" click" in line for line in link.written) == 1

            # 2. Allowed Send flow via skill-prepare-send -> click-observed --target-label 发送 -> skill-confirm-send.
            now[0] += 10
            screen._on_jpeg(jpeg())
            code, prep = await asyncio.to_thread(run_cli, "skill-prepare-send", "--skill", "wechat-group-reply")
            assert code == 0 and prep["allowed"] is True
            obs_id = controls.observation()["screen_id"]
            code, sent_res = await asyncio.to_thread(
                run_cli, "click-observed", ".5", ".4", "--screen-id", obs_id,
                "--target-box", ".4", ".3", ".6", ".5", "--target-label", "发送")
            assert code == 0 and sent_res["command_acknowledged"] is True
            assert len(guard.sends) == 1 and sum(b" click" in line for line in link.written) == 2
            code, conf = await asyncio.to_thread(
                run_cli, "skill-confirm-send", "--bubble-visible", "--skill", "wechat-group-reply")
            assert code == 0 and conf["counted"] is True and len(guard.sends) == 1

            # 3. Min-interval rate limit blocks labeled Send with zero HID clicks.
            now[0] += 1
            screen._on_jpeg(jpeg("white"))
            obs_id = controls.observation()["screen_id"]
            code, err = await asyncio.to_thread(
                run_cli, "click-observed", ".5", ".4", "--screen-id", obs_id,
                "--target-box", ".4", ".3", ".6", ".5", "--target-label", "Send")
            assert code == 2 and "8 秒" in err["error"]
            assert sum(b" click" in line for line in link.written) == 2

            # 4. Hold on Send is blocked with zero press/click writes.
            now[0] += 10
            screen._on_jpeg(jpeg())
            obs_id = controls.observation()["screen_id"]
            code, err = await asyncio.to_thread(
                run_cli, "click-observed", ".5", ".4", "--screen-id", obs_id,
                "--target-box", ".4", ".3", ".6", ".5", "--target-label", "发送", "--duration", "0.8")
            assert code == 2 and "hold" in err["error"].lower()
            assert not any(b"press" in line for line in link.written)
            assert sum(b" click" in line for line in link.written) == 2

            # 5. Quiet hours (23:30 for wechat-group-reply 23:00-07:00) blocks prepare + Send with zero clicks.
            now[0] = datetime(2026, 11, 15, 23, 30).timestamp()
            screen._on_jpeg(jpeg("black"))
            code, quiet = await asyncio.to_thread(run_cli, "skill-prepare-send", "--skill", "wechat-group-reply")
            assert code == 0 and quiet["allowed"] is False
            obs_id = controls.observation()["screen_id"]
            code, err = await asyncio.to_thread(
                run_cli, "click-observed", ".5", ".4", "--screen-id", obs_id,
                "--target-box", ".4", ".3", ".6", ".5", "--target-label", "发送")
            assert code == 2 and "静默" in err["error"]
            assert sum(b" click" in line for line in link.written) == 2

            # 6. Circuit breaker (skill-check -> stop=True) blocks click-observed and tap-text Send with zero clicks.
            now[0] = datetime(2026, 11, 16, 12, 0).timestamp()
            screen._on_jpeg(jpeg())
            code, chk = await asyncio.to_thread(
                run_cli, "skill-check", "--observed-text", "操作频繁", "--skill", "wechat-group-reply")
            assert code == 0 and chk["stop"] is True and guard.tripped is True
            obs_id = controls.observation()["screen_id"]
            code, err = await asyncio.to_thread(
                run_cli, "click-observed", ".5", ".4", "--screen-id", obs_id,
                "--target-box", ".4", ".3", ".6", ".5", "--target-label", "发送")
            assert code == 2 and "熔断" in err["error"]
            obs_id = controls.observation()["screen_id"]
            code, err2 = await asyncio.to_thread(
                run_cli, "tap-text", "发送", "--screen-id", obs_id, "--exact", "--region", "0", ".8", "1", "1")
            assert code == 2 and "熔断" in err2["error"]
            assert sum(b" click" in line for line in link.written) == 2
        finally:
            device.close()

    asyncio.run(scenario())


def test_point_cli_forwards_rehome_and_target_box(monkeypatch, capsys):
    seen = []

    class FakeClient:
        def __init__(self, _url): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False

        def control(self, name, screen_id, **fields):
            seen.append((name, screen_id, fields))
            return {"command_acknowledged": True, "pointer_seen": True}

    monkeypatch.setattr(cli, "IPhoneClient", FakeClient)
    code = cli.main(["point", "0.25", "0.92", "--screen-id", "s1",
                     "--rehome", "--target-box", "0.13", "0.90", "0.76", "0.95"])
    assert code == 0 and json.loads(capsys.readouterr().out)["pointer_seen"] is True
    assert seen == [("point", "s1", {
        "x": 0.25, "y": 0.92, "coordinate_space": "normalized",
        "target_box": [0.13, 0.9, 0.76, 0.95], "rehome": True,
    })]
