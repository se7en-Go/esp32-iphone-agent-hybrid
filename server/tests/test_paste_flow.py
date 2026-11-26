"""Host paste flow ordering and the read-only status poll; no phone or OCR backend."""

import asyncio
import base64
import io

import pytest
from PIL import Image, ImageDraw
from test_assistive_vision import button_frame
from test_staged_clipboard import prepare_body, setup

from hidagent import assistive_vision, ocr, paste_flow


def test_light_status_has_no_image_and_keeps_the_current_ticket(monkeypatch):
    async def scenario():
        _, _, _, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        ticket = control.observation()["screen_id"]
        light = manager.status(prepared["transaction_id"], include_observation=False)
        assert light["phase"] == "awaiting_copy" and "image_base64" not in light and "screen_id" not in light
        assert control.ticket[0] == ticket  # polling does not replace the host's observation
        full = manager.status(prepared["transaction_id"])
        assert full["image_base64"] and full["screen_id"] != ticket
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def jpeg(banner=False, pasted=False):
    image = Image.new("RGB", (50, 108), "white")
    if banner:
        ImageDraw.Draw(image).rectangle((5, 8, 45, 22), fill=(60, 60, 60))
    if pasted:
        ImageDraw.Draw(image).rectangle((10, 58, 35, 61), fill=(30, 30, 30))
    output = io.BytesIO()
    image.save(output, "JPEG")
    return output.getvalue()


PLAIN_JPEG = jpeg()
BANNER_JPEG = jpeg(banner=True)
PASTED_JPEG = jpeg(pasted=True)
SHEET_JPEG = jpeg(banner=True, pasted=True)
FIELD = (.13, .52, .77, .58)
TEXT = "第一行\n第二行"
HOLD = ("click", "辅助触控", .9)

PAGE_HEADER = ocr.TextBox("实验笔记本", (.12, .06, .42, .10), (.27, .08), .99)
SHEET_BOXES = [
    ocr.TextBox("微信想要从快捷指令粘贴", (.20, .43, .80, .48), (.50, .455), .98),
    ocr.TextBox("允许粘贴", (.22, .54, .42, .57), (.32, .557), .98),
    ocr.TextBox("不允许粘贴", (.58, .54, .78, .57), (.68, .557), .98),
    PAGE_HEADER,
]


class FakeClient:
    def __init__(self, *, confirm_conflicts=1, sheet=True, trigger_configured=False,
                 copied_on_prepare=False, overlay_frames=0, paste_conflicts=0, never_copy=False,
                 never_claim=False, allow_measured_pointer="auto", allow_dismiss_after=1,
                 pasted_visible=True):
        self.calls, self.control_calls, self.click_calls = [], [], []
        self.confirm_conflicts, self.sheet, self.seen = confirm_conflicts, sheet, 0
        self.trigger_configured = trigger_configured
        self.copied_on_prepare = copied_on_prepare
        self.overlay_frames = overlay_frames
        self.paste_conflicts, self.never_copy, self.never_claim = paste_conflicts, never_copy, never_claim
        self.allow_measured_pointer = allow_measured_pointer
        self.allow_dismiss_after = allow_dismiss_after
        self.pasted_visible = pasted_visible
        self.find_text_calls = []
        self.screenshots = 0
        self.pointer_proof = {}
        self.bubble_moves = []
        self.holds_at_prepare = 0

    def capabilities(self):
        return {"features": {"point_measurement": True, "clipboard": {"trigger_configured": self.trigger_configured}}}

    def frame(self, raw, *, for_click=False):
        self.seen += 1
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
        return {**self.pointer_proof, "screen_id": f"s{self.seen}", "frame_received_at": float(self.seen),
                "for_click": for_click,
                "width": width, "height": height, "image_base64": base64.b64encode(raw).decode()}

    def bubble_scene(self):
        return PLAIN_JPEG

    def bubble_landing(self, x, y):
        return {"x": x + .005, "y": y + .003}

    def observe(self, for_click=False):
        clicks = self.calls.count(("click", "允许粘贴", None))
        if self.sheet and "paste" in self.calls and clicks < self.allow_dismiss_after:
            raw = SHEET_JPEG
        elif self.pasted_visible and "paste" in self.calls and not for_click:
            raw = PASTED_JPEG
        else:
            raw = self.bubble_scene()
        return self.frame(raw, for_click=for_click)

    def screenshot(self):
        self.screenshots += 1
        if "status" in self.calls and self.overlay_frames > 0:
            self.overlay_frames -= 1
            return BANNER_JPEG
        return self.bubble_scene()

    def control(self, name, screen_id, **fields):
        self.calls.append(name)
        self.control_calls.append((name, fields))
        if name == "point" and "paste" in self.calls:
            self.pointer_proof = {}
            if self.allow_measured_pointer == "auto":
                mp = {"x": fields["x"] + .005, "y": fields["y"] + .003}
            else:
                mp = self.allow_measured_pointer
            if mp is not None:
                self.pointer_proof = {"measured_pointer": mp, "pointer_seen": True,
                                      "pointer_measurement_id": f"allow-{self.seen}"}
            return {**self.frame(SHEET_JPEG), "command_acknowledged": True, "fresh": True}
        if name == "point":
            self.bubble_moves.append((fields["x"], fields["y"]))
            self.pointer_proof = {"pointer_measurement_id": f"move-{len(self.bubble_moves)}",
                                  "pointer_seen": True,
                                  "measured_pointer": self.bubble_landing(fields["x"], fields["y"])}
            return {**self.frame(self.bubble_scene()), "fresh": True, "command_acknowledged": True}
        return {"command_acknowledged": True}

    def click_observed(self, screen_id, x, y, box, **fields):
        self.calls.append(("click", fields.get("target_label"), fields.get("duration")))
        self.click_calls.append((x, y, box, fields))
        self.pointer_proof = {}
        return {"command_acknowledged": True, "timing_ms": {"total": 1}}

    def find_text(self, text, screen_id, **fields):
        self.find_text_calls.append((text, screen_id, fields))
        clicks = self.calls.count(("click", "允许粘贴", None))
        if self.sheet and text == "允许粘贴" and "paste" in self.calls and clicks < self.allow_dismiss_after:
            return {"match": {"text": text, "center": [.32, .557], "box": [.22, .54, .42, .57]}}
        return {"match": None}

    def clipboard_stage(self, stage, **fields):
        self.calls.append(stage)
        if stage == "prepare":
            self.holds_at_prepare = self.calls.count(HOLD)
            return {"transaction_id": "tx"}
        if stage == "status":
            assert fields.get("include_observation") is False
            if "paste" in self.calls and fields.get("transaction_id") == "tx":
                return {"transaction_id": "tx", "phase": "awaiting_confirmation", "copied": True,
                        "paste_sent": True, "expires_in_seconds": 30.}
            copied = not self.never_copy and (self.copied_on_prepare or self.claimed)
            return {"copied": copied, "claimed": self.claimed or copied,
                    "phase": "awaiting_paste" if copied else "awaiting_copy"}
        if stage == "paste":
            if self.paste_conflicts:
                self.paste_conflicts -= 1
                raise RuntimeError("HTTP 409: screen changed since the original observation; observe again")
            return {"command_acknowledged": True}
        if stage == "confirm":
            if self.confirm_conflicts:
                self.confirm_conflicts -= 1
                raise RuntimeError("HTTP 409: confirmation requires a fresh post-paste observation")
            return {"phase": "completed", "effect_verified": True,
                    "confirmation_evidence": "paste_permission_dismissed"}
        if stage == "cancel":
            # The real server blocks only once the Shortcut has claimed the text.
            return {"active": False, "needs_resolution": self.claimed}
        raise AssertionError(stage)

    @property
    def claimed(self):
        return self.calls.count(HOLD) > self.holds_at_prepare and not self.never_claim


@pytest.fixture
def offline_ocr(monkeypatch):
    monkeypatch.setattr(paste_flow.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ocr, "read_text", lambda frame: (
        list(SHEET_BOXES) if frame == SHEET_JPEG else (
            [ocr.TextBox("第一行", (.16, .525, .40, .548), (.28, .536), .98),
             ocr.TextBox("第二行", (.16, .552, .40, .575), (.28, .563), .98)]
            if frame == PASTED_JPEG else [PAGE_HEADER]
        )
    ))


@pytest.fixture
def vision(monkeypatch, offline_ocr):
    monkeypatch.setattr(assistive_vision, "find_button_target", lambda frame: ((.75, .95), (.69, .92, .83, .98)))


def test_flow_pastes_once_handles_sheet_and_reconfirms_same_transaction(vision):
    client = FakeClient()
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert client.calls.count("paste") == 1
    assert client.calls.count("confirm") == 2 and report["retries"] == {"confirm": 1}
    assert ("click", "允许粘贴", None) in client.calls
    assert report["phase"] == "completed" and report["permission_sheet_seen"] is True
    assert report["cmd_v_sent"] == 1
    assert client.calls.index("prepare") < client.calls.index(("click", "辅助触控", .9)) < client.calls.index("paste")


def test_flow_keeps_an_existing_draft_and_pastes_nothing(vision, monkeypatch):
    monkeypatch.setattr(ocr, "read_text", lambda frame: [
        ocr.TextBox("草稿", (.2, .53, .3, .56), (.25, .545), .99)])
    client = FakeClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "文本", (.13, .52, .77, .58))
    assert "draft" in str(stopped.value) and stopped.value.report.facts["existing_draft_lines"] == 1
    assert not any(call in client.calls for call in ("prepare", "paste", "confirm"))


def test_flow_never_repastes_when_confirmation_keeps_failing(vision):
    client = FakeClient(confirm_conflicts=5)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58))
    assert client.calls.count("paste") == 1 and "cancel" not in client.calls
    facts = stopped.value.report.facts
    assert facts["transaction_left_open"] is True
    assert facts["cmd_v_sent"] == 1 and facts["recovery_state"] == "paste_sent_unconfirmed"
    assert "Never paste again" in facts["next_action"]


def test_flow_retries_a_pre_dispatch_paste_409_and_keeps_each_frame(vision, tmp_path):
    client = FakeClient(paste_conflicts=1)
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58), evidence_dir=tmp_path).as_dict()
    assert client.calls.count("paste") == 2  # one rejected before dispatch, one sent
    assert report["cmd_v_sent"] == 1 and report["phase"] == "completed"
    assert report["retries"]["paste"] == 1 and "cancel" not in client.calls
    assert report["recovery_state"] == "completed" and report["copied"] is True
    assert (tmp_path / "1-prepare-original.jpg").exists()
    assert (tmp_path / "2-paste-original.jpg").exists() and (tmp_path / "2-paste-original-1.jpg").exists()


def test_flow_reports_copied_not_pasted_when_paste_stays_rejected(vision):
    client = FakeClient(paste_conflicts=paste_flow.PASTE_ATTEMPTS)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "文本", (.13, .52, .77, .58))
    facts = stopped.value.report.facts
    assert client.calls.count("paste") == paste_flow.PASTE_ATTEMPTS and client.calls.count("cancel") == 1
    assert facts["copied"] is True and facts["cmd_v_sent"] == 0
    assert facts["cancelled"]["needs_resolution"] is True
    assert facts["recovery_state"] == "copied_not_pasted" and "Do not rerun paste-flow" in facts["next_action"]


@pytest.mark.parametrize(("never_claim", "state"), [(True, "not_copied"), (False, "claimed_not_confirmed")])
def test_flow_reports_copy_failure_by_whether_the_shortcut_claimed_the_text(vision, never_claim, state):
    client = FakeClient(never_copy=True, never_claim=never_claim)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "文本", (.13, .52, .77, .58), copy_timeout=0)
    facts = stopped.value.report.facts
    assert "paste" not in client.calls
    assert facts["copied"] is False and facts["cmd_v_sent"] == 0
    assert facts["cancelled"]["needs_resolution"] is (not never_claim)
    assert facts["recovery_state"] == state


def test_flow_records_pure_assistive_touch_trigger_when_trigger_key_disabled(vision):
    client = FakeClient(trigger_configured=False, copied_on_prepare=False)
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert report["trigger_configured"] is False
    assert report["copy_trigger"] == "assistive_touch_hold"
    assert ("click", "辅助触控", .9) in client.calls
    assert report["cmd_v_sent"] == 1 and report["phase"] == "completed"


def test_flow_attributes_trigger_key_when_prepare_already_copied(vision):
    client = FakeClient(trigger_configured=True, copied_on_prepare=True)
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert report["trigger_configured"] is True
    assert report["copy_trigger"] == "trigger_key"
    assert ("click", "辅助触控", .9) not in client.calls
    assert report["cmd_v_sent"] == 1 and report["phase"] == "completed"


def test_trigger_key_waits_for_copy_without_falling_back_to_a_hold(vision):
    client = FakeClient(trigger_configured=True, never_copy=True)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD, copy_timeout=0)
    assert client.calls.count("prepare") == 1 and "status" in client.calls
    assert HOLD not in client.calls and "paste" not in client.calls
    assert stopped.value.report.facts["cmd_v_sent"] == 0


@pytest.mark.parametrize("already_copied", [False, True])
def test_existing_shortcut_claim_or_copy_prevents_a_duplicate_trigger(vision, already_copied):
    class ClaimedClient(FakeClient):
        def clipboard_stage(self, stage, **fields):
            result = super().clipboard_stage(stage, **fields)
            if stage == "status":
                copied = already_copied or self.calls.count("status") > 1
                return {"phase": "awaiting_paste" if copied else "awaiting_copy",
                        "claimed": True, "copied": copied}
            return result

    client = ClaimedClient(trigger_configured=False, sheet=False, confirm_conflicts=0)
    report = paste_flow.run(client, TEXT, FIELD).as_dict()
    assert "status" in client.calls and HOLD not in client.calls
    assert client.calls.count("prepare") == client.calls.count("paste") == report["cmd_v_sent"] == 1


def test_flow_waits_for_transient_shortcut_banner_before_paste(vision):
    client = FakeClient(overlay_frames=2)
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert client.overlay_frames == 0
    assert "wait_overlay_clear" in report["stages_ms"]
    assert client.calls.count("paste") == 1 and report["phase"] == "completed"
    name, point = client.control_calls[0]
    assert name == "point" and point["rehome"] is point["measure_only"] is True
    assert (point["x"], point["y"]) == pytest.approx((.75, .95))
    left, top, right, bottom = point["target_box"]
    assert left < point["x"] < right and top < point["y"] < bottom


def assert_stopped_before_prepare(client, stopped):
    assert "prepare" not in client.calls and HOLD not in client.calls
    assert "paste" not in client.calls and "confirm" not in client.calls
    facts = stopped.value.report.facts
    assert facts["recovery_state"] == "not_copied" and facts["copied"] is False
    assert facts["cmd_v_sent"] == 0
    assert 1 <= len(client.bubble_moves) <= 2


@pytest.mark.parametrize("mode", ["ack_only", "echo_only", "unseen", "stale", "no_frame"])
def test_bubble_move_requires_positive_independent_evidence(vision, mode):
    class NoProofClient(FakeClient):
        def control(self, name, screen_id, **fields):
            receipt = super().control(name, screen_id, **fields)
            if mode == "ack_only":
                self.pointer_proof.clear()
                return {"command_acknowledged": True}
            if mode == "echo_only":
                self.pointer_proof.clear()
                return {**self.frame(self.bubble_scene()), "fresh": True, "command_acknowledged": True,
                        "x": fields["x"], "y": fields["y"]}
            if mode == "unseen":
                self.pointer_proof["pointer_seen"] = receipt["pointer_seen"] = False
            elif mode == "stale":
                receipt["fresh"] = False
            else:
                receipt.pop("image_base64")
            return receipt

    client = NoProofClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD)
    assert_stopped_before_prepare(client, stopped)


@pytest.mark.parametrize("prewarm", [False, True])
def test_every_bubble_hold_clicks_measured_point_instead_of_requested_center(vision, prewarm):
    client = FakeClient(sheet=False, confirm_conflicts=0)
    report = paste_flow.run(client, TEXT, FIELD, prewarm=prewarm).as_dict()
    assert report["phase"] == "completed" and report["cmd_v_sent"] == 1
    clicks = [(x, y) for x, y, box, args in client.click_calls if args.get("target_label") == "辅助触控"]
    assert len(clicks) == (2 if prewarm else 1)
    for x, y in clicks:
        assert (x, y) == pytest.approx((.755, .953))
    assert client.calls.count("prepare") == client.calls.count("paste") == 1


@pytest.mark.parametrize("mode", ["other_measurement", "same_frame", "changed_coordinate", "no_current_proof"])
def test_independent_observation_is_bound_to_the_current_move(vision, mode):
    class MismatchedClient(FakeClient):
        def control(self, name, screen_id, **fields):
            self.move_frame = super().control(name, screen_id, **fields)
            return self.move_frame

        def observe(self, for_click=False):
            observed = super().observe(for_click)
            if self.bubble_moves:
                if mode == "other_measurement":
                    observed["pointer_measurement_id"] = "different-move-or-session"
                elif mode == "same_frame":
                    observed["screen_id"] = self.move_frame["screen_id"]
                    observed["frame_received_at"] = self.move_frame["frame_received_at"]
                elif mode == "changed_coordinate":
                    observed["measured_pointer"] = {"x": .71, "y": .93}
                else:
                    observed.pop("pointer_measurement_id", None)
            return observed

    client = MismatchedClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD)
    assert_stopped_before_prepare(client, stopped)


@pytest.mark.parametrize("second_hits", [True, False])
def test_bubble_corrects_once_after_confirmed_miss(offline_ocr, second_hits):
    size = (500, 1080)
    raw = button_frame((.89, .455), size)
    target = assistive_vision.track_button_target(raw)
    positions = []

    class MissClient(FakeClient):
        def bubble_scene(self):
            return raw

        def bubble_landing(self, x, y):
            requested = x * size[0], y * size[1]
            fields = self.control_calls[-1][1]
            if fields.get("correct_from_measurement_id") is None:
                # Absolute motion reproduces the original 40 px vertical bias.
                px, py = requested[0], requested[1] + 40
            else:
                assert fields["correct_from_measurement_id"] == self.pointer_proof["pointer_measurement_id"]
                px, py = positions[-1]
                gain = .9 if second_hits else 0.0  # An effective relative HID move or a stalled one.
                px += gain * (requested[0] - px)
                py += gain * (requested[1] - py)
            positions.append((px, py))
            return {"x": px / size[0], "y": py / size[1]}

    client = MissClient(sheet=False, confirm_conflicts=0)
    if second_hits:
        report = paste_flow.run(client, TEXT, FIELD).as_dict()
        assert report["phase"] == "completed" and report["cmd_v_sent"] == 1
        held_x, held_y, click_box, _ = client.click_calls[0]
        assert (held_x * size[0], held_y * size[1]) == pytest.approx(positions[-1])
        assert click_box == pytest.approx(target.safe_box)
    else:
        with pytest.raises(paste_flow.FlowStopped) as stopped:
            paste_flow.run(client, TEXT, FIELD)
        assert_stopped_before_prepare(client, stopped)
    assert len(client.bubble_moves) == len(positions) == 2
    assert "correct_from_measurement_id" not in client.control_calls[0][1]
    assert client.control_calls[1][1]["correct_from_measurement_id"] == "move-1"
    first_error, second_error = [py - target.point[1] * size[1] for _, py in positions]
    assert first_error == pytest.approx(40)
    assert second_error == pytest.approx(4 if second_hits else 40)
    for _name, fields in client.control_calls:
        assert fields["measure_only"] is True
        assert (fields["x"], fields["y"]) == pytest.approx(target.point)
        assert fields["target_box"] == pytest.approx(target.probe_box)
        assert fields["target_box"][0] < target.safe_box[0]
        assert fields["target_box"][2] > target.safe_box[2]


@pytest.mark.parametrize("destination", [(.91, .36), (.09, .64)])
@pytest.mark.parametrize("move_after", ["point", "prepare"])
def test_moving_button_reaims_before_prepare_and_stops_after_prepare(offline_ocr, destination, move_after):
    initial = (.91, .64)
    images = {initial: button_frame(initial), destination: button_frame(destination)}

    class MovingClient(FakeClient):
        def bubble_scene(self):
            return images[destination if move_after in self.calls else initial]

    client = MovingClient(sheet=False, confirm_conflicts=0)
    if move_after == "prepare":
        with pytest.raises(paste_flow.FlowStopped) as stopped:
            paste_flow.run(client, TEXT, FIELD)
        assert client.calls.count("prepare") == client.calls.count("cancel") == 1
        assert "paste" not in client.calls and HOLD not in client.calls
        assert stopped.value.report.facts["recovery_state"] == "not_copied"
        return

    report = paste_flow.run(client, TEXT, FIELD).as_dict()
    assert len(client.bubble_moves) == 2
    assert client.bubble_moves[0] == pytest.approx(initial, abs=.01)
    assert client.bubble_moves[1] == pytest.approx(destination, abs=.01)
    x, y, box, fields = client.click_calls[0]
    assert fields["target_label"] == "辅助触控"
    assert (x, y) == pytest.approx(destination, abs=.012)
    assert box[0] < x < box[2] and box[1] < y < box[3]
    assert client.calls.count(HOLD) == client.calls.count("paste") == report["cmd_v_sent"] == 1


def test_legacy_point_schema_rejection_does_not_bypass_positive_evidence(vision):
    class LegacyClient(FakeClient):
        def control(self, name, screen_id, **fields):
            assert fields["measure_only"] is True
            if "rehome" in fields or "target_box" in fields:
                self.calls.append("legacy_schema_rejection")
                raise RuntimeError("HTTP 422: unknown field rehome")
            super().control(name, screen_id, **fields)
            self.pointer_proof.clear()
            return {"command_acknowledged": True}

    client = LegacyClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD)
    assert "legacy_schema_rejection" in client.calls
    assert_stopped_before_prepare(client, stopped)


def test_old_server_rejecting_measure_only_never_receives_a_plain_point(vision):
    class LegacyClient(FakeClient):
        def control(self, name, screen_id, **fields):
            self.control_calls.append((name, fields))
            if fields.get("measure_only"):
                raise RuntimeError("HTTP 422: unknown field measure_only")
            return super().control(name, screen_id, **fields)

    client = LegacyClient()
    with pytest.raises(paste_flow.FlowStopped):
        paste_flow.run(client, TEXT, FIELD)
    assert len(client.control_calls) == 2
    first, last = [fields for _, fields in client.control_calls]
    assert first["rehome"] is first["measure_only"] is last["measure_only"] is True
    assert "target_box" in first and "target_box" not in last and "rehome" not in last
    assert not client.bubble_moves and "prepare" not in client.calls and HOLD not in client.calls


def test_old_server_that_ignores_unknown_fields_receives_no_input(vision):
    client = FakeClient()
    client.capabilities = lambda: {"features": {"clipboard": {"trigger_configured": False}}}
    with pytest.raises(paste_flow.FlowStopped, match="point_measurement support"):
        paste_flow.run(client, TEXT, FIELD)
    assert client.control_calls == [] and client.calls == []


@pytest.mark.parametrize("stale_field", ["screen_id", "frame_received_at", "pointer_measurement_id"])
def test_correction_requires_new_frame_of_that_same_miss(vision, stale_field):
    class UncorroboratedMissClient(FakeClient):
        def bubble_landing(self, x, y):
            return {"x": .75, "y": .99}

        def control(self, name, screen_id, **fields):
            self.move_receipt = super().control(name, screen_id, **fields)
            return self.move_receipt

        def observe(self, for_click=False):
            observed = super().observe(for_click)
            if self.bubble_moves:
                observed[stale_field] = ("another-move" if stale_field == "pointer_measurement_id"
                                         else self.move_receipt[stale_field])
            return observed

    client = UncorroboratedMissClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD)
    assert_stopped_before_prepare(client, stopped)
    assert len(client.bubble_moves) == 1


@pytest.mark.parametrize("defect", ["different_measurement", "normal_ticket"])
def test_stale_click_ticket_after_prepare_never_dispatches_a_hold(vision, defect):
    class StaleClickClient(FakeClient):
        def observe(self, for_click=False):
            result = super().observe(for_click)
            if for_click and "prepare" in self.calls:
                if defect == "different_measurement":
                    result["pointer_measurement_id"] = "after-another-action-or-session"
                else:
                    result["for_click"] = False
            return result

    client = StaleClickClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD)
    assert client.calls.count("prepare") == client.calls.count("cancel") == 1
    assert HOLD not in client.calls and "paste" not in client.calls
    assert stopped.value.report.facts["cmd_v_sent"] == 0


@pytest.mark.parametrize("mode", ["timeout_after_dispatch", "cancelled_409", "unacked_may_have_executed",
                                  "missing_write_boundary"])
def test_hold_with_ambiguous_dispatch_is_never_repeated(vision, mode):
    class AmbiguousHoldClient(FakeClient):
        def click_observed(self, screen_id, x, y, box, **fields):
            super().click_observed(screen_id, x, y, box, **fields)
            if mode == "timeout_after_dispatch":
                raise TimeoutError("read timed out after dispatch")
            if mode == "cancelled_409":
                raise RuntimeError("HTTP 409: host click stopped; observe before continuing")
            if mode == "missing_write_boundary":
                return {"command_acknowledged": False, "error": "未发送原地点击"}
            return {"command_acknowledged": False, "action_may_have_executed": True,
                    "error": "未发送原地点击 (unreliable message after timeout)"}

    client = AmbiguousHoldClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD)
    assert client.calls.count(HOLD) == 1
    assert client.calls.count("prepare") == 1 and "paste" not in client.calls
    facts = stopped.value.report.facts
    assert facts["cmd_v_sent"] == 0 and facts["hold_may_have_executed"] is True
    assert "Do not automatically repeat" in facts["next_action"]


@pytest.mark.parametrize("lose_measurement", [True, False])
def test_predispatch_hold_rejection_requires_a_new_verified_ticket(vision, lose_measurement):
    class RejectedHoldClient(FakeClient):
        def __init__(self):
            super().__init__(sheet=False)
            self.tickets = []

        def click_observed(self, screen_id, x, y, box, **fields):
            self.tickets.append(screen_id)
            if len(self.tickets) == 1:
                if lose_measurement:
                    self.pointer_proof.clear()
                return {"command_acknowledged": False, "action_may_have_executed": False,
                        "error": "等待点击期间原画面已变化"}
            return super().click_observed(screen_id, x, y, box, **fields)

    client = RejectedHoldClient()
    if lose_measurement:
        with pytest.raises(paste_flow.FlowStopped):
            paste_flow.run(client, TEXT, FIELD)
        assert len(client.tickets) == 1 and HOLD not in client.calls and "paste" not in client.calls
    else:
        result = paste_flow.run(client, TEXT, FIELD).as_dict()
        assert len(set(client.tickets)) == len(client.tickets) == 2
        assert result["retries"]["shortcut_hold"] == client.calls.count(HOLD) == result["cmd_v_sent"] == 1


def test_bubble_keeps_caret_evidence_scoped_to_the_verified_field(vision, monkeypatch):
    client = FakeClient(sheet=False, confirm_conflicts=0)
    scheduler_box = (.14, .53, .16, .56)
    monkeypatch.setattr(paste_flow, "wait_caret_plateau", lambda client, box: box == scheduler_box)
    paste_flow.run(client, TEXT, FIELD, caret_box=scheduler_box)
    assert client.control_calls[0][1]["caret_scope"] == list(FIELD)
    assert client.click_calls[0][3]["caret_scope"] == list(FIELD)


def test_flow_clicks_measured_allow_pointer_not_ocr_center(vision):
    client = FakeClient()
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert report["phase"] == "completed" and report["permission_sheet_dismissed"] is True
    allow_points = [fields for name, fields in client.control_calls if name == "point" and fields["x"] < .5]
    assert len(allow_points) == 1 and "target_box" in allow_points[0]
    allow_clicks = [(x, y) for x, y, _box, fields in client.click_calls if fields.get("target_label") == "允许粘贴"]
    assert allow_clicks == [pytest.approx((.325, .560))]


def test_flow_stops_in_permission_sheet_when_allow_click_ack_does_not_dismiss(vision):
    client = FakeClient(allow_dismiss_after=99)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58))
    assert "did not dismiss" in str(stopped.value)
    assert client.calls.count("paste") == 1
    assert client.calls.count(("click", "允许粘贴", None)) == 1
    assert "confirm" not in client.calls and "cancel" not in client.calls
    facts = stopped.value.report.facts
    assert facts["permission_sheet_seen"] is True and facts["permission_sheet_dismissed"] is False
    assert facts["cmd_v_sent"] == 1 and facts["transaction_left_open"] is True
    assert facts["recovery_state"] == "paste_sent_unconfirmed"


def test_flow_waits_for_allow_dismissal_without_a_second_click(vision):
    class AnimatingClient(FakeClient):
        post_click_frames = 0

        def observe(self, for_click=False):
            if ("click", "允许粘贴", None) in self.calls and self.post_click_frames < 2:
                self.post_click_frames += 1
                return self.frame(SHEET_JPEG, for_click=for_click)
            return super().observe(for_click=for_click)

    client = AnimatingClient()
    report = paste_flow.run(client, TEXT, FIELD).as_dict()
    assert client.calls.count("paste") == client.calls.count(("click", "允许粘贴", None)) == 1
    assert report["permission_sheet_seen"] is True and report["permission_sheet_dismissed"] is True
    assert report["phase"] == "completed"


@pytest.mark.parametrize("measured", [None, {"x": .70, "y": .557}])
def test_flow_refuses_blind_allow_click_when_pointer_not_confirmed_in_button(vision, measured):
    client = FakeClient(allow_measured_pointer=measured)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58))
    assert "not confirmed" in str(stopped.value)
    assert client.calls.count("paste") == 1
    assert ("click", "允许粘贴", None) not in client.calls
    assert "confirm" not in client.calls
    facts = stopped.value.report.facts
    assert facts["permission_sheet_seen"] is True and facts["permission_sheet_dismissed"] is False
    assert facts["cmd_v_sent"] == 1 and facts["recovery_state"] == "paste_sent_unconfirmed"


def test_flow_rejects_an_unmeasured_dark_disc_on_the_allow_button(vision, monkeypatch):
    def sheet_image(*, dot_xy=None):
        image = Image.new("RGB", (360, 778), (242, 242, 244))
        if dot_xy is not None:
            cx, cy = round(dot_xy[0] * 360), round(dot_xy[1] * 778)
            ImageDraw.Draw(image).ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(120, 120, 120))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=92)
        return out.getvalue()

    clean_sheet = sheet_image()
    pointed_sheet = sheet_image(dot_xy=(.330, .567))
    base_read = ocr.read_text
    monkeypatch.setattr(ocr, "read_text", lambda frame: list(SHEET_BOXES) if frame == clean_sheet else base_read(frame))

    class VisualAllowClient(FakeClient):
        def observe(self, for_click=False):
            if for_click and "paste" in self.calls:
                raw = pointed_sheet
            elif "paste" in self.calls and self.calls.count(("click", "允许粘贴", None)) >= self.allow_dismiss_after:
                raw = PASTED_JPEG
            elif "paste" in self.calls:
                raw = clean_sheet
            else:
                raw = PLAIN_JPEG
            return self.frame(raw, for_click=for_click)

        def clipboard_stage(self, stage, **fields):
            res = super().clipboard_stage(stage, **fields)
            if stage == "paste":
                res["screen_id"] = "s_sheet"
                res["image_base64"] = base64.b64encode(clean_sheet).decode()
            return res

    client = VisualAllowClient(allow_measured_pointer=None)
    with pytest.raises(paste_flow.FlowStopped, match="not confirmed"):
        paste_flow.run(client, TEXT, FIELD)
    assert client.calls.count("paste") == 1 and ("click", "允许粘贴", None) not in client.calls


def test_flow_refuses_confirm_when_pasted_text_is_not_observed(vision):
    client = FakeClient(pasted_visible=False, sheet=False)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58), confirm_attempts=2)
    assert "not observed" in str(stopped.value)
    assert "confirm" not in client.calls and "cancel" not in client.calls
    facts = stopped.value.report.facts
    assert facts["cmd_v_sent"] == 1 and facts["transaction_left_open"] is True
    assert facts["recovery_state"] == "paste_sent_unconfirmed"
    assert stopped.value.report.retries["confirm_text"] == 1


@pytest.mark.parametrize("drift", ["title", "draft"])
def test_flow_rechecks_title_and_empty_draft_on_prepare_frame(monkeypatch, drift):
    monkeypatch.setattr(assistive_vision, "find_button_target", lambda _f: ((.75, .95), (.69, .92, .83, .98)))
    monkeypatch.setattr(paste_flow.time, "sleep", lambda _s: None)
    initial_jpg = PLAIN_JPEG
    drifted_jpg = BANNER_JPEG

    def read_text(frame):
        if frame == initial_jpg:
            return [ocr.TextBox("测试群", (.35, .06, .65, .09), (.50, .075), .99)]
        if drift == "title":
            return [ocr.TextBox("其他群", (.35, .06, .65, .09), (.50, .075), .99)]
        return [
            ocr.TextBox("测试群", (.35, .06, .65, .09), (.50, .075), .99),
            ocr.TextBox("人工刚打的草稿", (.18, .53, .45, .56), (.31, .545), .99),
        ]

    monkeypatch.setattr(ocr, "read_text", read_text)

    class DriftClient(FakeClient):
        def observe(self, for_click=False):
            # First observe is initial check; after point_bubble, prepare_observe sees drifted frame.
            raw = drifted_jpg if "point" in self.calls else initial_jpg
            return self.frame(raw, for_click=for_click)

    client = DriftClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58), expect_title="测试群")
    assert "prepare" not in client.calls and "paste" not in client.calls
    assert stopped.value.report.facts["recovery_state"] == "not_copied"
    if drift == "title":
        assert "title" in str(stopped.value)
    else:
        assert "draft" in str(stopped.value)


@pytest.mark.parametrize("mode", ["unacked_200", "transport_504"])
def test_flow_treats_ambiguous_paste_dispatch_as_paste_sent_unconfirmed(vision, mode):
    class AmbiguousPasteClient(FakeClient):
        def clipboard_stage(self, stage, **fields):
            if stage == "paste":
                self.calls.append(stage)
                if mode == "unacked_200":
                    return {"command_acknowledged": False, "action_may_have_executed": True,
                            "error": "serial ack lost"}
                raise RuntimeError("HTTP 504: operation deadline exceeded")
            return super().clipboard_stage(stage, **fields)

    client = AmbiguousPasteClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58))
    assert client.calls.count("paste") == 1
    assert "cancel" not in client.calls
    facts = stopped.value.report.facts
    assert facts["action_may_have_executed"] is True
    assert facts["recovery_state"] == "paste_sent_unconfirmed"


def test_permission_match_ignores_allow_label_in_header(monkeypatch):
    outside = ocr.TextBox("允许粘贴", (.40, .06, .60, .10), (.5, .08), .99)
    monkeypatch.setattr(ocr, "read_text", lambda _: [SHEET_BOXES[0], outside, SHEET_BOXES[2]])
    assert paste_flow._allow_target(SHEET_JPEG) is None


def test_flow_ignores_plain_allow_paste_text_without_permission_sheet(vision, monkeypatch):
    """Ordinary page text saying '允许粘贴' inside ALLOW_MODAL_REGION must never be clicked."""
    fake_page = jpeg(banner=False, pasted=False) + b"\x00"
    base_read = ocr.read_text

    def read_page(frame):
        if frame == fake_page:
            return [ocr.TextBox("允许粘贴", (.22, .54, .42, .57), (.32, .557), .98)]
        return base_read(frame)

    monkeypatch.setattr(ocr, "read_text", read_page)

    class PlainPageClient(FakeClient):
        def __init__(self):
            super().__init__(sheet=False, confirm_conflicts=0)
            self.post_paste_observes = 0

        def find_text(self, text, screen_id, **fields):
            self.find_text_calls.append((text, screen_id, fields))
            if text == "允许粘贴" and "paste" in self.calls:
                return {"match": {"text": text, "center": [.32, .557], "box": [.22, .54, .42, .57]}}
            return {"match": None}

        def observe(self, for_click=False):
            self.seen += 1
            if "paste" in self.calls and not for_click:
                self.post_paste_observes += 1
                if self.post_paste_observes <= 4:
                    return {"screen_id": f"s{self.seen}", "image_base64": base64.b64encode(fake_page).decode()}
            return super().observe(for_click=for_click)

    client = PlainPageClient()
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert report["permission_sheet_seen"] is False
    assert ("click", "允许粘贴", None) not in client.calls


@pytest.mark.parametrize("kind", ["read_timeout", "disk_fail_after_ack", "connect_error", "http_422"])
def test_flow_classifies_httpx_transport_and_post_ack_disk_errors(vision, tmp_path, kind):
    import httpx

    class TransportClient(FakeClient):
        def clipboard_stage(self, stage, **fields):
            if stage == "paste":
                self.calls.append(stage)
                if kind == "read_timeout":
                    raise httpx.ReadTimeout("timed out waiting for paste reply")
                if kind == "connect_error":
                    raise httpx.ConnectError("connection refused")
                if kind == "http_422":
                    raise RuntimeError("HTTP 422: field validation error")
                return {"command_acknowledged": True,
                        "image_base64": base64.b64encode(PASTED_JPEG).decode()}
            return super().clipboard_stage(stage, **fields)

    client = TransportClient(sheet=False)
    ev_dir = tmp_path / "evidence"
    if kind == "disk_fail_after_ack":
        # Make 3-after-cmd-v.jpg a directory so writing it raises OSError after ACK.
        ev_dir.mkdir()
        (ev_dir / "3-after-cmd-v.jpg").mkdir()

    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58),
                       evidence_dir=ev_dir if kind == "disk_fail_after_ack" else None)
    facts = stopped.value.report.facts
    if kind in ("read_timeout", "disk_fail_after_ack"):
        assert "cancel" not in client.calls
        assert facts["recovery_state"] == "paste_sent_unconfirmed"
        assert facts["transaction_left_open"] is True
    else:
        assert "cancel" in client.calls
        assert facts["recovery_state"] == "copied_not_pasted"


def test_verify_confirm_frame_edges_and_scrolled_long_text(monkeypatch):
    region = (.13, .45, .77, .60)
    boxes = []
    monkeypatch.setattr(ocr, "read_text", lambda _frame: list(boxes))

    # 1. Whitespace-only text must not silently verify as True.
    ok, reason = paste_flow._verify_confirm_frame(None, PASTED_JPEG, region, "   \n  ")
    assert ok is False and "visible" in reason

    # 2. Low-confidence OCR box must be rejected even in relaxed center matching.
    boxes[:] = [ocr.TextBox("你好世界", (.16, .47, .50, .50), (.33, .485), .55)]
    ok, _ = paste_flow._verify_confirm_frame(None, PASTED_JPEG, region, "你好世界")
    assert ok is False

    # 3. Substantially overlapping external box across boundary must be rejected.
    boxes[:] = [
        ocr.TextBox("你好世界", (.16, .47, .50, .50), (.33, .485), .98),
        ocr.TextBox("被截断的额外行", (.16, .58, .50, .63), (.33, .605), .98),
    ]
    ok, _ = paste_flow._verify_confirm_frame(None, PASTED_JPEG, region, "你好世界")
    assert ok is False

    # 4. Long 40-line pasted text scrolled to tail 4 visible rows must verify as suffix.
    long_text = "\n".join(f"第{i}行测试文本" for i in range(1, 41))
    boxes[:] = [
        ocr.TextBox("第37行测试文本", (.16, .46, .55, .485), (.35, .472), .98),
        ocr.TextBox("第38行测试文本", (.16, .49, .55, .515), (.35, .502), .98),
        ocr.TextBox("第39行测试文本", (.16, .52, .55, .545), (.35, .532), .98),
        ocr.TextBox("第40行测试文本", (.16, .55, .55, .575), (.35, .562), .98),
    ]
    ok, mode = paste_flow._verify_confirm_frame(None, PASTED_JPEG, region, long_text)
    assert ok is True and mode == "suffix"

    # 5. Long 200-char single line horizontally scrolled to tail 20 chars must verify as suffix.
    long_line = "这是一段很长的单行回复内容" * 16 + "结尾二十个字校验锚点在这里"
    boxes[:] = [ocr.TextBox("结尾二十个字校验锚点在这里", (.16, .50, .70, .53), (.43, .515), .98)]
    ok, mode = paste_flow._verify_confirm_frame(None, PASTED_JPEG, region, long_line)
    assert ok is True and mode == "suffix"

    # 6. Short 2-line text with only 1 line visible must still be rejected.
    boxes[:] = [ocr.TextBox("第二行", (.16, .50, .40, .53), (.28, .515), .98)]
    ok, _ = paste_flow._verify_confirm_frame(None, PASTED_JPEG, region, "第一行\n第二行")
    assert ok is False


@pytest.mark.parametrize("change", [None, "gone", "source", "page", "button"])
def test_allow_refresh_after_two_unmeasured_moves_clicks_once_or_stops(vision, monkeypatch, change):
    """Two real point receipts preceded the historical stale third ticket."""
    client = FakeClient(confirm_conflicts=0, trigger_configured=True, copied_on_prepare=True)
    original_control, original_observe = client.control, client.observe
    points = 0
    refreshed = False
    changed = b"changed-permission-scene"
    boxes = list(SHEET_BOXES)
    if change == "gone":
        boxes = [PAGE_HEADER]
    elif change == "source":
        boxes[0] = ocr.TextBox("微信想要从相册粘贴", boxes[0].box, boxes[0].center, .98)
    elif change == "page":
        boxes[-1] = ocr.TextBox("另一个群", PAGE_HEADER.box, PAGE_HEADER.center, .99)
    elif change == "button":
        boxes[1] = ocr.TextBox("允许粘贴", (.46, .54, .50, .57), (.48, .557), .98)
    original_read = ocr.read_text
    monkeypatch.setattr(ocr, "read_text", lambda raw: boxes if raw == changed else original_read(raw))

    def point(name, screen_id, **fields):
        nonlocal points, refreshed
        if name == "point" and "paste" in client.calls:
            points += 1
            if points == 3:
                refreshed = True
                raise RuntimeError("HTTP 409: screen changed since observation; observe again")
            client.allow_measured_pointer = None if points < 3 else "auto"
        return original_control(name, screen_id, **fields)

    def observe(for_click=False):
        if refreshed and change is not None:
            return {**original_observe(for_click), "image_base64": base64.b64encode(changed).decode()}
        return original_observe(for_click)

    monkeypatch.setattr(client, "control", point)
    monkeypatch.setattr(client, "observe", observe)
    if change is None:
        report = paste_flow.run(client, TEXT, FIELD, confirm_attempts=1).as_dict()
        assert report["phase"] == "completed" and report["effect_verified"]
        assert len(client.click_calls) == 1
        assert client.calls.count("confirm") == 1
        assert points == 4
    else:
        with pytest.raises(paste_flow.FlowStopped, match="permission prompt changed"):
            paste_flow.run(client, TEXT, FIELD, confirm_attempts=1)
        assert client.click_calls == []
        assert "confirm" not in client.calls
    assert client.calls.count("paste") == 1


@pytest.mark.parametrize("failure", ["stale_forever", "click_unknown", "initial_wrong_source"])
def test_allow_refresh_is_bounded_and_never_repeats_uncertain_click(vision, monkeypatch, failure):
    client = FakeClient(confirm_conflicts=0, trigger_configured=True, copied_on_prepare=True)
    points = clicks = 0
    original_control = client.control
    if failure == "initial_wrong_source":
        original_read = ocr.read_text
        prompt = SHEET_BOXES[0]
        changed = [ocr.TextBox("微信想要从相册粘贴", prompt.box, prompt.center, .98), *SHEET_BOXES[1:]]
        monkeypatch.setattr(ocr, "read_text", lambda raw: changed if raw == SHEET_JPEG else original_read(raw))

    def point(name, screen_id, **fields):
        nonlocal points
        if name == "point":
            points += 1
            if failure == "stale_forever":
                raise RuntimeError("HTTP 409: screen changed since observation; observe again")
        return original_control(name, screen_id, **fields)

    def click(*args, **kwargs):
        nonlocal clicks
        clicks += 1
        raise RuntimeError("Allow Paste click dispatch is uncertain")

    monkeypatch.setattr(client, "control", point)
    monkeypatch.setattr(client, "click_observed", click)
    with pytest.raises(paste_flow.FlowStopped):
        paste_flow.run(client, TEXT, FIELD, confirm_attempts=1)
    assert client.calls.count("paste") == 1
    assert "confirm" not in client.calls
    assert clicks == (1 if failure == "click_unknown" else 0)
    assert points == {"stale_forever": 3, "click_unknown": 1, "initial_wrong_source": 0}[failure]


def test_paste_flow_exits_permission_check_early_once_direct_paste_verified(vision, monkeypatch):
    toast_frame = b"toast-banner-over-header"
    original_read = ocr.read_text
    pasted_boxes = list(original_read(PASTED_JPEG))
    toast_boxes = [ocr.TextBox("微信-粘贴自：快捷指令", PAGE_HEADER.box, PAGE_HEADER.center, .98), *pasted_boxes]
    pasted_with_header = [PAGE_HEADER, *pasted_boxes]
    monkeypatch.setattr(ocr, "read_text", lambda raw: (
        toast_boxes if raw == toast_frame else (pasted_with_header if raw == PASTED_JPEG else original_read(raw))
    ))

    class DirectToastClient(FakeClient):
        def __init__(self):
            super().__init__(sheet=False, confirm_conflicts=0, trigger_configured=True, copied_on_prepare=True)
            self.post_paste_observes = 0

        def clipboard_stage(self, stage, **fields):
            result = super().clipboard_stage(stage, **fields)
            if stage == "paste":
                return {**result, "image_base64": base64.b64encode(toast_frame).decode()}
            return result

        def observe(self, for_click=False):
            if "paste" in self.calls:
                self.post_paste_observes += 1
            return super().observe(for_click=for_click)

    client = DirectToastClient()
    report = paste_flow.run(client, TEXT, FIELD, expect_title="实验笔记本", confirm_attempts=1).as_dict()
    assert report["phase"] == "completed" and report["effect_verified"] is True
    assert client.calls.count("confirm") == 1
    # One post-paste observe clears the header toast; one observe runs confirm
    # (vs 4 + 1 without early exit when ALLOW_CHECK_ATTEMPTS == 4).
    assert client.post_paste_observes == 2


def _chat_flow_frame(*, pasted: bool, key_blue: bool = False, grown: bool = True):
    image = Image.new("RGB", (500, 1080), (237, 237, 237))
    draw = ImageDraw.Draw(image)
    top = 480 if (pasted and grown) else 562
    draw.rectangle((66, top, 382, 618), fill="white")
    if pasted:
        draw.rectangle((92, top + 16, 320, top + 24), fill=(35, 35, 35))
        draw.rectangle((92, top + 56, 300, top + 64), fill=(35, 35, 35))
    draw.rectangle((0, 626, 500, 700), fill=(214, 217, 222))
    draw.rectangle((10, 646, 490, 676), fill=(214, 217, 222))
    draw.rectangle((0, 700, 500, 1080), fill=(214, 217, 222))
    send_fill = (10, 132, 255) if key_blue else (200, 204, 212)
    draw.rounded_rectangle((380, 924, 494, 974), radius=8, fill=send_fill)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue()


def test_verify_confirm_frame_tail_punct_gates_and_field_span_fallback(monkeypatch):
    from hidagent import paste_confirmation

    field = (66 / 500, 562 / 1080, 382 / 500, 618 / 1080)
    text = "这期具体保质期每款不一样，你点开对应链接看下后问群主确认。"
    prep = _chat_flow_frame(pasted=False, key_blue=False, grown=False)
    conf = _chat_flow_frame(pasted=True, key_blue=True, grown=True)
    stale_prep = _chat_flow_frame(pasted=False, key_blue=True, grown=False)

    header = ocr.TextBox("测试群(10)", (.35, .06, .55, .09), (.45, .075), .99)
    kb = [
        ocr.TextBox("发送", (.83, .86, .93, .90), (.88, .88), .99),
        ocr.TextBox("123", (.03, .86, .12, .90), (.08, .88), .99),
    ]
    grown_items = [
        ocr.TextBox("这期具体保质期每款不一样，", (.16, .46, .66, .49), (.41, .475), .99),
        ocr.TextBox("你点开对应链接看下后问群主确认", (.16, .51, .70, .54), (.43, .525), .99),
    ]
    boxes = {
        prep: [header, ocr.TextBox("候选A", (.15, .60, .25, .63), (.20, .615), .99), *kb],
        stale_prep: [header, ocr.TextBox("候选A", (.15, .60, .25, .63), (.20, .615), .99), *kb],
        conf: [header, *grown_items, ocr.TextBox("候选B", (.15, .60, .25, .63), (.20, .615), .99), *kb],
    }
    monkeypatch.setattr(ocr, "read_text", lambda raw: boxes[raw])

    # Default allow_tail_punct=False rejects even valid tail_punct; allow_tail_punct=True accepts.
    diag_f = {}
    assert paste_flow._verify_confirm_frame(
        prep, conf, field, text, expect_title="测试群(10)", allow_tail_punct=False, diagnostics=diag_f,
    ) == (False, "pasted text was not observed in target field")
    assert diag_f["expected_chars"] == len(text) and diag_f["observed_chars"] == len(text) - 1

    diag_t = {}
    assert paste_flow._verify_confirm_frame(
        prep, conf, field, text, expect_title="测试群(10)", allow_tail_punct=True, diagnostics=diag_t,
    ) == (True, "tail_punct")
    assert diag_t["elided_tail"] == "。" and diag_t["chat_composer_verified"] is True

    # Force _grown_field to return None (e.g. AssistiveTouch orb in side band): _field_span still recovers region.
    monkeypatch.setattr(paste_confirmation, "_grown_field", lambda *_a, **_kw: None)
    assert paste_flow._verify_confirm_frame(
        prep, conf, field, text, expect_title="测试群(10)", allow_tail_punct=True,
    ) == (True, "tail_punct")

    # Stale already-blue send key in prepare_frame must block tail_punct.
    assert paste_flow._verify_confirm_frame(
        stale_prep, conf, field, text, expect_title="测试群(10)", allow_tail_punct=True,
    ) == (False, "pasted text was not observed in target field")

    # Extra line inside the grown region must block tail_punct even if a sub-region matches.
    conf_extra_items = [header, *grown_items, ocr.TextBox("多余冲突行", (.16, .545, .45, .565), (.30, .555), .99), *kb]
    monkeypatch.setattr(ocr, "read_text", lambda raw: conf_extra_items if raw == conf else boxes[raw])
    assert paste_flow._verify_confirm_frame(
        prep, conf, field, text, expect_title="测试群(10)", allow_tail_punct=True,
    ) == (False, "pasted text was not observed in target field")


def test_paste_flow_confirm_diagnostics_populated_and_exact_bytes_reused(monkeypatch):
    frame = _chat_flow_frame(pasted=True, key_blue=True, grown=False)
    frame_alt = _chat_flow_frame(pasted=True, key_blue=True, grown=False) + b"\x00"
    confirm_verify_calls = 0
    orig_verify = paste_flow._verify_confirm_frame

    def spy_verify(*args, **kwargs):
        nonlocal confirm_verify_calls
        if kwargs.get("diagnostics") is not None:
            confirm_verify_calls += 1
        return orig_verify(*args, **kwargs)

    monkeypatch.setattr(paste_flow, "_verify_confirm_frame", spy_verify)
    monkeypatch.setattr(ocr, "read_text", lambda _raw: [])
    monkeypatch.setattr(assistive_vision, "find_button_target", lambda _f: ((.75, .95), (.69, .92, .83, .98)))

    class ConfirmClient(FakeClient):
        def __init__(self, *, alternate_after_first: bool = False):
            super().__init__(confirm_conflicts=0, sheet=False, trigger_configured=True, copied_on_prepare=True)
            self._alt = alternate_after_first
            self._confirm_obs = 0

        def observe(self, for_click=False):
            self.seen += 1
            if "paste" in self.calls and confirm_verify_calls >= 1:
                self._confirm_obs += 1
            raw = frame_alt if (self._alt and self._confirm_obs >= 1) else frame
            return {"screen_id": f"s-{self.seen}", "image_base64": base64.b64encode(raw).decode()}

    # Identical frame bytes reuse the failed verify result across retries.
    client = ConfirmClient(alternate_after_first=False)
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, "第一行说明文字\n第二行补充说明。", FIELD, confirm_attempts=4)
    assert confirm_verify_calls == 1
    assert stopped.value.report.retries["confirm_text"] == 3
    diag = stopped.value.report.facts["confirm_diagnostics"]
    assert diag["expected_chars"] == 15 and diag["observed_chars"] == 0

    # Visually similar but byte-distinct confirm frames must still re-run OCR rather than short-circuiting.
    confirm_verify_calls = 0
    distinct_client = ConfirmClient(alternate_after_first=True)
    with pytest.raises(paste_flow.FlowStopped):
        paste_flow.run(distinct_client, "第一行说明文字\n第二行补充说明。", FIELD, confirm_attempts=3)
    assert confirm_verify_calls == 2


def test_verify_confirm_frame_filters_stationary_icon_only(monkeypatch):
    field = (66 / 500, 562 / 1080, 382 / 500, 618 / 1080)
    text = "这个得看具体商品，拿不准直接问群主确认。"
    icon_box = (.69, .545, .725, .562)
    icon_px = (348, 591, 359, 604)

    def make_frame(*, pasted: bool, draw_icon_before: bool, draw_icon_after: bool, with_keyboard: bool = True):
        image = Image.new("RGB", (500, 1080), (237, 237, 237))
        draw = ImageDraw.Draw(image)
        top = 480 if pasted else 562
        draw.rectangle((66, top, 382, 618), fill="white")
        if pasted:
            draw.rectangle((92, top + 16, 320, top + 24), fill=(35, 35, 35))
        if (draw_icon_after if pasted else draw_icon_before):
            draw.rectangle(icon_px, fill=(55, 55, 55))
        if with_keyboard:
            draw.rectangle((0, 626, 500, 700), fill=(214, 217, 222))
            draw.rectangle((10, 646, 490, 676), fill=(214, 217, 222))
            draw.rectangle((0, 700, 500, 1080), fill=(214, 217, 222))
            draw.rounded_rectangle((380, 924, 494, 974), radius=8, fill=(10, 132, 255) if pasted else (200, 204, 212))
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    header = ocr.TextBox("测试群(10)", (.35, .06, .55, .09), (.45, .075), .99)
    kb = [
        ocr.TextBox("发送", (.83, .86, .93, .90), (.88, .88), .99),
        ocr.TextBox("123", (.03, .86, .12, .90), (.08, .88), .99),
    ]
    grown_items = [
        ocr.TextBox("这个得看具体商品，", (.16, .46, .66, .49), (.41, .475), .99),
        ocr.TextBox("拿不准直接问群主确认。", (.16, .54, .68, .565), (.42, .552), .99),
        ocr.TextBox("U", icon_box, (.707, .553), .97),
    ]
    prep_stationary = make_frame(pasted=False, draw_icon_before=True, draw_icon_after=True)
    conf_stationary = make_frame(pasted=True, draw_icon_before=True, draw_icon_after=True)
    prep_blank = make_frame(pasted=False, draw_icon_before=False, draw_icon_after=True)
    conf_new_u = make_frame(pasted=True, draw_icon_before=False, draw_icon_after=True)
    prep_nokb = make_frame(pasted=False, draw_icon_before=True, draw_icon_after=True, with_keyboard=False)
    conf_nokb = make_frame(pasted=True, draw_icon_before=True, draw_icon_after=True, with_keyboard=False)

    boxes = {
        prep_stationary: [header, *kb],
        conf_stationary: [header, *grown_items, *kb],
        prep_blank: [header, *kb],
        conf_new_u: [header, *grown_items, *kb],
        prep_nokb: [header],
        conf_nokb: [header, *grown_items],
    }
    monkeypatch.setattr(ocr, "read_text", lambda raw: boxes[raw])

    diag = {}
    assert paste_flow._verify_confirm_frame(
        prep_stationary, conf_stationary, field, text, expect_title="测试群(10)",
        allow_tail_punct=False, diagnostics=diag,
    ) == (True, "full")
    assert diag["filtered_icons"] == 1
    assert paste_flow._verify_confirm_frame(
        prep_blank, conf_new_u, field, text, expect_title="测试群(10)", allow_tail_punct=True,
    ) == (False, "pasted text was not observed in target field")
    # Non-chat layout (no keyboard strip) MUST NOT filter bottom-right UI items.
    assert paste_flow._verify_confirm_frame(
        prep_nokb, conf_nokb, field, text, expect_title="测试群(10)", allow_tail_punct=False,
    ) == (False, "pasted text was not observed in target field")
    # Non-empty prepare OCR MUST NOT filter stationary items.
    monkeypatch.setattr(ocr, "read_text", lambda raw: (
        [header, ocr.TextBox("U", icon_box, (.707, .553), .97), *kb] if raw == prep_stationary else boxes[raw]
    ))
    assert paste_flow._verify_confirm_frame(
        prep_stationary, conf_stationary, field, text, expect_title="测试群(10)", allow_tail_punct=False,
    ) == (False, "pasted text was not observed in target field")


def test_verify_confirm_frame_corner_fade_requires_physical_bleach_and_gate(monkeypatch):
    field = (66 / 500, 562 / 1080, 382 / 500, 618 / 1080)
    text = "这个得赶紧找群主确认，没发货前一般还能协调改，具体看订单状态。"
    icon_px = (348, 591, 359, 604)

    def make_frame(*, pasted: bool, faded_corner: bool):
        image = Image.new("RGB", (500, 1080), (237, 237, 237))
        draw = ImageDraw.Draw(image)
        top = 514 if pasted else 562
        draw.rectangle((66, top, 382, 618), fill="white")
        draw.rectangle(icon_px, fill=(45, 45, 45))
        if pasted:
            draw.rectangle((140, 558, 332, 582), fill=(15, 15, 15))
            draw.rectangle((335, 558, 356, 568), fill=(70, 70, 70))
            draw.rectangle((335, 570, 356, 582), fill=(155, 155, 155) if faded_corner else (10, 10, 10))
        draw.rectangle((0, 626, 500, 700), fill=(214, 217, 222))
        draw.rectangle((10, 646, 490, 676), fill=(214, 217, 222))
        draw.rectangle((0, 700, 500, 1080), fill=(214, 217, 222))
        draw.rounded_rectangle((380, 924, 494, 974), radius=8, fill=(10, 132, 255) if pasted else (200, 204, 212))
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    header = ocr.TextBox("测试群(10)", (.35, .06, .55, .09), (.45, .075), .99)
    kb = [
        ocr.TextBox("发送", (.83, .86, .93, .90), (.88, .88), .99),
        ocr.TextBox("123", (.03, .86, .12, .90), (.08, .88), .99),
    ]
    grown_items = [
        ocr.TextBox("这个得赶紧找群主确认，没", (.158, .492, .713, .515), (.435, .503), .99),
        ocr.TextBox("发货前一般还能协调改，目", (.156, .514, .713, .542), (.434, .528), .98),
        ocr.TextBox("体看订单状态。", (.158, .542, .459, .565), (.308, .553), .99),
    ]
    prep = make_frame(pasted=False, faded_corner=False)
    conf_faded = make_frame(pasted=True, faded_corner=True)
    conf_crisp = make_frame(pasted=True, faded_corner=False)
    boxes = {prep: [header, *kb], conf_faded: [header, *grown_items, *kb], conf_crisp: [header, *grown_items, *kb]}
    monkeypatch.setattr(ocr, "read_text", lambda raw: boxes[raw])

    diag = {}
    assert paste_flow._verify_confirm_frame(
        prep, conf_faded, field, text, expect_title="测试群(10)", allow_tail_punct=True, diagnostics=diag,
    ) == (True, "corner_fade")
    assert diag["corner_fade_diff"] == "具->目" and diag["chat_composer_verified"] is True

    assert paste_flow._verify_confirm_frame(
        prep, conf_faded, field, text, expect_title="测试群(10)", allow_tail_punct=False,
    ) == (False, "pasted text was not observed in target field")
    assert paste_flow._verify_confirm_frame(
        prep, conf_crisp, field, text, expect_title="测试群(10)", allow_tail_punct=True,
    ) == (False, "pasted text was not observed in target field")

    # Stacked missing thin CJK bracket '「' + corner_fade passes when faded & allowed, rejects otherwise.
    stacked_text = "这个「得赶紧找群主确认，没发货前一般还能协调改，具体看订单状态。"
    omitted_row1 = ocr.TextBox("这个得赶紧找群主确认，没", (.158, .492, .713, .515), (.435, .503), .99)
    stacked_boxes = {
        prep: [header, *kb],
        conf_faded: [header, omitted_row1, grown_items[1], grown_items[2], *kb],
        conf_crisp: [header, omitted_row1, grown_items[1], grown_items[2], *kb],
    }
    monkeypatch.setattr(ocr, "read_text", lambda raw: stacked_boxes[raw])
    diag_s = {}
    assert paste_flow._verify_confirm_frame(
        prep, conf_faded, field, stacked_text, expect_title="测试群(10)", allow_tail_punct=True, diagnostics=diag_s,
    ) == (True, "corner_fade")
    assert diag_s["corner_fade_diff"] == "具->目;punct=「"
    assert paste_flow._verify_confirm_frame(
        prep, conf_faded, field, stacked_text, expect_title="测试群(10)", allow_tail_punct=False,
    ) == (False, "pasted text was not observed in target field")
    assert paste_flow._verify_confirm_frame(
        prep, conf_crisp, field, stacked_text, expect_title="测试群(10)", allow_tail_punct=True,
    ) == (False, "pasted text was not observed in target field")
