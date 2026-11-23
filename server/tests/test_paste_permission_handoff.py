"""A stale move may be recovered, but one Cmd+V never licenses two uncertain clicks."""

import base64

import pytest
from test_paste_flow import FIELD, PASTED_JPEG, SHEET_BOXES, SHEET_JPEG, TEXT, FakeClient, offline_ocr, vision  # noqa: F401

from hidagent import ocr, paste_flow

pytestmark = pytest.mark.usefixtures("vision")

ALLOW_CLICK = ("click", "允许粘贴", None)


def test_permission_point_409_uses_fresh_ticket_without_sending_paste_again():
    class StaleClient(FakeClient):
        def __init__(self):
            super().__init__(confirm_conflicts=0)
            self.tickets = []

        def clipboard_stage(self, stage, **fields):
            result = super().clipboard_stage(stage, **fields)
            if stage == "paste":
                result.update(screen_id="old-post-paste", image_base64=base64.b64encode(SHEET_JPEG).decode())
            return result

        def control(self, name, screen_id, **fields):
            if name == "point" and "paste" in self.calls:
                self.tickets.append(screen_id)
                if len(self.tickets) == 1:
                    raise RuntimeError("HTTP 409: screen changed since observation; observe again")
                assert fields["measure_only"] is True
            return super().control(name, screen_id, **fields)

    client = StaleClient()
    receipt = paste_flow.run(client, TEXT, FIELD).as_dict()
    assert client.find_text_calls == []
    assert len(set(client.tickets)) == 2 and "old-post-paste" not in client.tickets
    assert client.calls.count("paste") == client.calls.count(ALLOW_CLICK) == client.calls.count("confirm") == 1
    assert receipt["retries"]["allow_paste_point"] == 1 and receipt["permission_sheet_dismissed"]


@pytest.mark.parametrize("change", ["deny", "plain_label", "other_app", "generic_allow", "header", "stale_proof",
                                    "replayed_frame", "outside_button", "overlap", "unqualified_ticket"])
def test_changed_click_evidence_never_dispatches_permission(monkeypatch, change):
    altered = SHEET_JPEG + b"test-ocr"
    items = list(SHEET_BOXES)
    read = ocr.read_text
    if change == "deny":
        items[1], items[2] = (ocr.TextBox(items[j].text, items[i].box, items[i].center, .99)
                             for i, j in ((1, 2), (2, 1)))
    elif change == "plain_label":
        items = items[1:]
    elif change == "other_app":
        items[0] = ocr.TextBox("备忘录想要从快捷指令粘贴", items[0].box, items[0].center, .99)
    elif change == "generic_allow":
        items[1] = ocr.TextBox("允许", items[1].box, items[1].center, .99)
    elif change == "header":
        items.append(ocr.TextBox("另一个页面", (.06, .08, .25, .13), (.15, .10), .99))
    elif change == "overlap":
        items[2] = ocr.TextBox(items[2].text, items[1].box, items[1].center, .99)
    monkeypatch.setattr(ocr, "read_text", lambda image: items if image == altered else read(image))

    class ChangedClient(FakeClient):
        def observe(self, for_click=False):
            result = super().observe(for_click=for_click)
            if for_click and "paste" in self.calls:
                result["image_base64"] = base64.b64encode(altered).decode()
                if change == "stale_proof":
                    result["pointer_measurement_id"] = "different-hid-move"
                elif change == "replayed_frame":
                    result["frame_received_at"] = 1.
                elif change == "outside_button":
                    result["measured_pointer"] = {"x": .68, "y": .56}
                elif change == "unqualified_ticket":
                    result["for_click"] = False
            return result

    client = ChangedClient()
    with pytest.raises(paste_flow.FlowStopped) as stopped:
        paste_flow.run(client, TEXT, FIELD)
    assert ALLOW_CLICK not in client.calls and "confirm" not in client.calls
    assert client.calls.count("paste") == 1 and stopped.value.report.facts["transaction_left_open"]


@pytest.mark.parametrize("state", ["known_no_press", "unknown", "exception", "cancelled"])
def test_permission_dispatch_retries_only_a_proven_pre_press_rejection(state):
    class DispatchClient(FakeClient):
        permission_requests = 0

        def click_observed(self, screen_id, x, y, box, **fields):
            if fields.get("target_label") == "允许粘贴":
                self.permission_requests += 1
                if self.permission_requests == 1:
                    if state == "exception":
                        raise RuntimeError("HTTP 409: screen changed since observation")
                    return {"command_acknowledged": False,
                            "action_may_have_executed": state == "unknown",
                            "error": "操作取消，未发送点击" if state == "cancelled" else "等待点击期间原画面已变化"}
            return super().click_observed(screen_id, x, y, box, **fields)

    client = DispatchClient(confirm_conflicts=0)
    if state == "known_no_press":
        assert paste_flow.run(client, TEXT, FIELD).facts["phase"] == "completed"
        assert client.permission_requests == 2 and client.calls.count(ALLOW_CLICK) == 1
    else:
        with pytest.raises(paste_flow.FlowStopped):
            paste_flow.run(client, TEXT, FIELD)
        assert client.permission_requests == 1 and "confirm" not in client.calls
    assert client.calls.count("paste") == 1


def test_cancel_before_point_reacquisition_never_clicks_or_repastes():
    class CancelledClient(FakeClient):
        def clipboard_stage(self, stage, **fields):
            result = super().clipboard_stage(stage, **fields)
            if stage == "paste":
                result.update(screen_id="old", image_base64=base64.b64encode(SHEET_JPEG).decode())
            return result

        def observe(self, for_click=False):
            return self.frame(PASTED_JPEG) if "paste" in self.calls else super().observe(for_click=for_click)

    client = CancelledClient()
    with pytest.raises(paste_flow.FlowStopped, match="changed before pointing"):
        paste_flow.run(client, TEXT, FIELD)
    assert client.calls.count("paste") == 1 and ALLOW_CLICK not in client.calls and "confirm" not in client.calls


@pytest.mark.parametrize("stage", ["click", "miss"])
@pytest.mark.parametrize("mode", ["advances", "frozen", "replaced_measurement"])
def test_permission_waits_for_a_new_publication_of_the_same_move(stage, mode):
    class CachedClient(FakeClient):
        moves = 0
        observations = 0
        moved = None

        def control(self, name, screen_id, **fields):
            if name == "point" and "paste" in self.calls:
                self.moves += 1
                self.allow_measured_pointer = {"x": .68, "y": .56} if stage == "miss" and self.moves == 1 else "auto"
                assert "correct_from_measurement_id" not in fields
                self.moved = super().control(name, screen_id, **fields)
                return self.moved
            return super().control(name, screen_id, **fields)

        def observe(self, for_click=False):
            current = super().observe(for_click=for_click)
            if self.moves == 1 and self.moved and for_click == (stage == "click") and ALLOW_CLICK not in self.calls:
                self.observations += 1
                if self.observations <= 2 or mode == "frozen":
                    current["frame_received_at"] = self.moved["frame_received_at"]
                elif mode == "replaced_measurement":
                    current["pointer_measurement_id"] = "different-move"
            return current

    client = CachedClient(confirm_conflicts=0)
    if mode == "advances":
        receipt = paste_flow.run(client, TEXT, FIELD)
        assert receipt.facts["phase"] == "completed"
        assert client.moves == (2 if stage == "miss" else 1)
        assert client.calls.count(ALLOW_CLICK) == 1
        assert receipt.retries["allow_paste_publication"] == 2
    else:
        with pytest.raises(paste_flow.FlowStopped):
            paste_flow.run(client, TEXT, FIELD)
        assert client.moves == 1 and ALLOW_CLICK not in client.calls and "confirm" not in client.calls
    assert client.calls.count("paste") == 1


@pytest.mark.parametrize("expect_title", [None, "实验笔记本"])
@pytest.mark.parametrize("change", ["other_page", "no_identifying_header"])
def test_first_permission_prompt_must_belong_to_prepared_page(monkeypatch, expect_title, change):
    read = ocr.read_text
    title = "别的应用页面" if change == "other_page" else "完成"
    header = ocr.TextBox(title, (.12, .06, .42, .10), (.27, .08), .99)
    monkeypatch.setattr(ocr, "read_text", lambda image: [*SHEET_BOXES[:3], header] if image == SHEET_JPEG
                        else ([header] if change == "no_identifying_header" and not expect_title else read(image)))
    client = FakeClient(confirm_conflicts=0)
    with pytest.raises(paste_flow.FlowStopped, match="original page"):
        paste_flow.run(client, TEXT, FIELD, expect_title=expect_title)
    assert client.calls.count("paste") == 1 and ALLOW_CLICK not in client.calls and "confirm" not in client.calls


@pytest.mark.parametrize("state", ["nonce", "phase", "not_sent", "not_copied", "expired", "unknown_ttl", "gone"])
def test_permission_click_checks_same_live_paste_transaction_without_consuming_ticket(state):
    class InvalidTransaction(FakeClient):
        def clipboard_stage(self, stage, **fields):
            result = super().clipboard_stage(stage, **fields)
            if stage == "status" and "paste" in self.calls:
                assert fields == {"transaction_id": "tx", "include_observation": False}
                if state == "gone":
                    raise RuntimeError("HTTP 404: transaction expired")
                changes = {"nonce": {"transaction_id": "different"}, "phase": {"phase": "awaiting_paste"},
                           "not_sent": {"paste_sent": False}, "not_copied": {"copied": False},
                           "expired": {"expires_in_seconds": 0}, "unknown_ttl": {"expires_in_seconds": None}}
                result.update(changes[state])
            return result

    client = InvalidTransaction(confirm_conflicts=0)
    with pytest.raises(paste_flow.FlowStopped, match="transaction"):
        paste_flow.run(client, TEXT, FIELD)
    assert client.calls.count("paste") == 1 and ALLOW_CLICK not in client.calls and "confirm" not in client.calls


@pytest.mark.parametrize("generic", [False, True])
def test_pasted_body_resembling_a_permission_dialog_only_confirms(monkeypatch, generic):
    items = list(SHEET_BOXES)
    if generic:
        for index, label in ((1, "允许"), (2, "不允许")):
            items[index] = ocr.TextBox(label, items[index].box, items[index].center, .99)
    read = ocr.read_text
    monkeypatch.setattr(ocr, "read_text", lambda image: items if image == SHEET_JPEG else read(image))
    client = FakeClient(confirm_conflicts=0)
    text = items[0].text + "\n" + items[1].text + items[2].text
    report = paste_flow.run(client, text, (.17, .40, .85, .59), expect_title="实验笔记本")
    assert report.facts["phase"] == "completed" and not report.facts["permission_sheet_seen"]
    assert client.calls.count("paste") == client.calls.count("confirm") == 1 and ALLOW_CLICK not in client.calls
