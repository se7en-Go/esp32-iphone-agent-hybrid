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
                 copied_on_prepare=False, overlay_frames=0):
        self.calls, self.confirm_conflicts, self.sheet, self.seen = [], confirm_conflicts, sheet, 0
        self.trigger_configured = trigger_configured
        self.copied_on_prepare = copied_on_prepare
        self.overlay_frames = overlay_frames
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
        return {"command_acknowledged": True}

    def click_observed(self, screen_id, x, y, box, **fields):
        self.calls.append(("click", fields.get("target_label"), fields.get("duration")))
        return {"command_acknowledged": True, "timing_ms": {"total": 1}}

    def find_text(self, text, screen_id, **fields):
        pressed = ("click", "允许粘贴", None) in self.calls
        if self.sheet and text == "允许粘贴" and "paste" in self.calls and not pressed:
            return {"match": {"text": text, "center": [.32, .557], "box": [.22, .54, .42, .57]}}
        return {"match": None}

    def clipboard_stage(self, stage, **fields):
        self.calls.append(stage)
        if stage == "prepare":
            return {"transaction_id": "tx"}
        if stage == "status":
            assert fields.get("include_observation") is False
            copied = self.copied_on_prepare or ("click", "辅助触控", .9) in self.calls
            return {"copied": copied, "phase": "awaiting_paste" if copied else "awaiting_copy"}
        if stage == "paste":
            return {"command_acknowledged": True}
        if stage == "confirm":
            if self.confirm_conflicts:
                self.confirm_conflicts -= 1
                raise RuntimeError("HTTP 409: confirmation requires a fresh post-paste observation")
            return {"phase": "completed", "effect_verified": True,
                    "confirmation_evidence": "paste_permission_dismissed"}
        if stage == "cancel":
            return {"active": False, "needs_resolution": True}
        raise AssertionError(stage)


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
    assert stopped.value.report.facts["transaction_left_open"] is True


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
