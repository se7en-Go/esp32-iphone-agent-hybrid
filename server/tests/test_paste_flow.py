"""Host paste flow ordering and the read-only status poll; no phone or OCR backend."""

import asyncio
import base64
import io

import pytest
from PIL import Image, ImageDraw
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


def jpeg(banner=False):
    image = Image.new("RGB", (50, 108), "white")
    if banner:
        ImageDraw.Draw(image).rectangle((5, 8, 45, 22), fill=(60, 60, 60))
    output = io.BytesIO()
    image.save(output, "JPEG")
    return output.getvalue()


PLAIN_JPEG = jpeg()
BANNER_JPEG = jpeg(banner=True)


class FakeClient:
    def __init__(self, *, confirm_conflicts=1, sheet=True, trigger_configured=False,
                 copied_on_prepare=False, overlay_frames=0, paste_conflicts=0, never_copy=False,
                 never_claim=False, allow_measured_pointer="auto", allow_dismiss_after=1):
        self.calls, self.control_calls, self.click_calls = [], [], []
        self.confirm_conflicts, self.sheet, self.seen = confirm_conflicts, sheet, 0
        self.trigger_configured = trigger_configured
        self.copied_on_prepare = copied_on_prepare
        self.overlay_frames = overlay_frames
        self.paste_conflicts, self.never_copy, self.never_claim = paste_conflicts, never_copy, never_claim
        self.allow_measured_pointer = allow_measured_pointer
        self.allow_dismiss_after = allow_dismiss_after
        self.screenshots = 0

    def capabilities(self):
        return {"features": {"clipboard": {"trigger_configured": self.trigger_configured}}}

    def observe(self, for_click=False):
        self.seen += 1
        return {"screen_id": f"s{self.seen}", "image_base64": base64.b64encode(PLAIN_JPEG).decode()}

    def screenshot(self):
        self.screenshots += 1
        if "status" in self.calls and self.overlay_frames > 0:
            self.overlay_frames -= 1
            return BANNER_JPEG
        return PLAIN_JPEG

    def control(self, name, screen_id, **fields):
        self.calls.append(name)
        self.control_calls.append((name, fields))
        if name == "point" and "paste" in self.calls:
            if self.allow_measured_pointer == "auto":
                mp = {"x": fields["x"] + .005, "y": fields["y"] + .003}
            else:
                mp = self.allow_measured_pointer
            return {"command_acknowledged": True, "measured_pointer": mp, "pointer_seen": mp is not None}
        return {"command_acknowledged": True}

    def click_observed(self, screen_id, x, y, box, **fields):
        self.calls.append(("click", fields.get("target_label"), fields.get("duration")))
        self.click_calls.append((x, y, box, fields))
        return {"command_acknowledged": True, "timing_ms": {"total": 1}}

    def find_text(self, text, screen_id, **fields):
        clicks = self.calls.count(("click", "允许粘贴", None))
        if self.sheet and text == "允许粘贴" and "paste" in self.calls and clicks < self.allow_dismiss_after:
            return {"match": {"text": text, "center": [.32, .557], "box": [.22, .54, .42, .57]}}
        return {"match": None}

    def clipboard_stage(self, stage, **fields):
        self.calls.append(stage)
        if stage == "prepare":
            return {"transaction_id": "tx"}
        if stage == "status":
            assert fields.get("include_observation") is False
            copied = not self.never_copy and (self.copied_on_prepare or ("click", "辅助触控", .9) in self.calls)
            return {"copied": copied, "phase": "awaiting_paste" if copied else "awaiting_copy"}
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
        return ("click", "辅助触控", .9) in self.calls and not self.never_claim


@pytest.fixture
def vision(monkeypatch):
    monkeypatch.setattr(assistive_vision, "find_button_target", lambda frame: ((.75, .95), (.69, .92, .83, .98)))
    monkeypatch.setattr(paste_flow.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ocr, "read_text", lambda frame: [])


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
        paste_flow.run(client, "文本", (.13, .52, .77, .58))
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


def test_flow_waits_for_transient_shortcut_banner_before_paste(vision):
    client = FakeClient(overlay_frames=2)
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert client.overlay_frames == 0
    assert "wait_overlay_clear" in report["stages_ms"]
    assert client.calls.count("paste") == 1 and report["phase"] == "completed"
    assert client.control_calls[0] == ("point", {"x": .75, "y": .95, "target_box": [.69, .92, .83, .98],
                                                  "rehome": True})


@pytest.mark.parametrize("still_stray", [False, True])
def test_flow_corrects_or_aborts_before_hold_when_stray_cursor_lands_below_bubble(monkeypatch, still_stray):
    def bubble_scene(*, dot_xy=None):
        image = Image.new("RGB", (360, 778), "white")
        draw = ImageDraw.Draw(image)
        if dot_xy is not None:
            cx, cy = round(dot_xy[0] * 360), round(dot_xy[1] * 778)
            draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(110, 110, 110))
        output = io.BytesIO()
        image.save(output, "JPEG", quality=92)
        return output.getvalue()

    clean = bubble_scene()
    stray = bubble_scene(dot_xy=(.89, .41))
    inside = bubble_scene(dot_xy=(.89, .36))

    monkeypatch.setattr(assistive_vision, "find_button_target",
                        lambda _frame: ((.89, .36), (.82, .33, .96, .39)))
    monkeypatch.setattr(paste_flow.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ocr, "read_text", lambda _frame: [])

    class StrayClient(FakeClient):
        def observe(self, for_click=False):
            self.seen += 1
            return {"screen_id": f"s{self.seen}", "image_base64": base64.b64encode(clean).decode()}

        def control(self, name, screen_id, **fields):
            box = fields.get("target_box")
            # Same rule as Controls._plan: a supplied box must contain the point.
            if box is not None and not (box[0] < fields["x"] < box[2] and box[1] < fields["y"] < box[3]):
                raise RuntimeError("HTTP 409: target_box must contain the tap in normalized observed coordinates")
            return super().control(name, screen_id, **fields)

        def screenshot(self):
            self.screenshots += 1
            if self.calls.count("point") == 1:
                return stray
            return stray if still_stray else inside

    client = StrayClient()
    if still_stray:
        with pytest.raises(paste_flow.FlowStopped) as stopped:
            paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58))
        assert "outside AssistiveTouch" in str(stopped.value)
        assert stopped.value.report.retries["point_bubble_miss"] == 1
        assert ("click", "辅助触控", .9) not in client.calls
        assert "prepare" not in client.calls and "paste" not in client.calls
        assert stopped.value.report.facts["recovery_state"] == "not_copied"
    else:
        report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
        assert report["retries"]["point_bubble_miss"] == 1
        assert "point_bubble" not in report["retries"]  # the correction was not rejected
        bubble_points = [fields for name, fields in client.control_calls if name == "point" and fields["x"] > .8]
        assert len(bubble_points) == 2
        assert bubble_points[0]["rehome"] is True and bubble_points[0]["target_box"] == [.82, .33, .96, .39]
        assert "rehome" not in bubble_points[1]
        assert bubble_points[1]["y"] < .33 and "target_box" not in bubble_points[1]  # aim outside the box
        assert ("click", "辅助触控", .9) in client.calls
        assert report["phase"] == "completed"


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
    assert client.calls.count(("click", "允许粘贴", None)) == paste_flow.ALLOW_DISMISS_ATTEMPTS
    assert "confirm" not in client.calls and "cancel" not in client.calls
    facts = stopped.value.report.facts
    assert facts["permission_sheet_seen"] is True and facts["permission_sheet_dismissed"] is False
    assert facts["cmd_v_sent"] == 1 and facts["transaction_left_open"] is True
    assert facts["recovery_state"] == "paste_sent_unconfirmed"


def test_flow_recovers_when_first_allow_click_only_wakes_pointer_and_second_dismisses(vision):
    client = FakeClient(allow_dismiss_after=2)
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert client.calls.count("paste") == 1
    assert client.calls.count(("click", "允许粘贴", None)) == 2
    assert report["retries"]["allow_paste_dismiss"] == 1
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


def test_flow_locates_visible_allow_pointer_from_for_click_frame_when_measured_pointer_is_none(vision):
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

    class VisualAllowClient(FakeClient):
        def observe(self, for_click=False):
            self.seen += 1
            raw = pointed_sheet if (for_click and "paste" in self.calls) else clean_sheet
            return {"screen_id": f"s{self.seen}", "image_base64": base64.b64encode(raw).decode()}

        def clipboard_stage(self, stage, **fields):
            res = super().clipboard_stage(stage, **fields)
            if stage == "paste":
                res["screen_id"] = "s_sheet"
                res["image_base64"] = base64.b64encode(clean_sheet).decode()
            return res

    client = VisualAllowClient(allow_measured_pointer=None)
    report = paste_flow.run(client, "第一行\n第二行", (.13, .52, .77, .58)).as_dict()
    assert report["phase"] == "completed" and report["permission_sheet_dismissed"] is True
    allow_clicks = [(x, y) for x, y, _box, fields in client.click_calls if fields.get("target_label") == "允许粘贴"]
    assert len(allow_clicks) == 1
    assert allow_clicks[0] == pytest.approx((.330, .567), abs=.01)
