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
    (["paste-cancel", "tx-1"], "cancel", {"transaction_id": "tx-1"}),
])
def test_staged_paste_commands_keep_separate_evidence_boundaries(remote, capsys, argv, stage, body):
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["effect_verified"] is False
    assert remote == [("POST", f"/api/control/clipboard/{stage}", body)]
