"""Hardware mouse-button clipboard trigger and direct sheet-less paste confirmation."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_paste_confirmation import label
from test_paste_flow import PASTED_JPEG, FakeClient
from test_staged_clipboard import setup

from hidagent import assistive_vision, feedback, ocr, paste_confirmation, paste_flow
from hidagent.assistive import AssistiveTouch
from hidagent.clipboard import CompanionClipboard
from hidagent.clipboard_control import ConfirmPaste, PasteTransaction, PreparePaste
from hidagent.device import DeviceError

BASE = "http://clipboard.test:8891"
KEY = "test_pairing_key_not_a_secret_123456789"
WECHAT_FIELD = (.13, .52, .77, .58)


def _wechat_direct_frame(*, pasted=False, grown=False, extra_edit=False, send_already_blue=False,
                         header_tamper=False, keyboard_tamper=False,
                         incoming_group_msg=False, draft_tamper=False):
    """Synthetic WeChat-like screen (500x1080): chat above, input bar, keyboard below."""
    image = Image.new("RGB", (500, 1080), (237, 237, 237))
    draw = ImageDraw.Draw(image)
    # Header bar
    draw.rectangle((0, 0, 499, 108), fill=(237, 237, 237))
    draw.line((0, 108, 499, 108), fill=(210, 210, 210), width=1)
    if header_tamper:
        draw.rectangle((40, 72, 120, 96), fill=(220, 30, 30))
    # Chat bubble shifted up by 48px when grown=True
    shift = 48 if grown else 0
    draw.rounded_rectangle((60, 260 - shift, 310, 320 - shift), radius=10, fill="white")
    draw.rounded_rectangle((180, 380 - shift, 440, 440 - shift), radius=10, fill=(149, 236, 105))
    if incoming_group_msg:
        draw.rounded_rectangle((70, 150, 350, 208), radius=10, fill="white")
        draw.rectangle((90, 172, 260, 186), fill=(30, 30, 30))
    # Input bar area (grows upward by 48px when grown=True)
    field_top = 562 - shift
    draw.rectangle((0, field_top - 10, 499, 636), fill=(246, 246, 246))
    draw.rectangle((65, field_top, 385, 626), fill="white")
    if pasted:
        draft_color = (220, 30, 30) if draft_tamper else (30, 30, 30)
        draw.rectangle((80, field_top + 16, 240, field_top + 28), fill=draft_color)
        if grown:
            draw.rectangle((80, field_top + 52, 260, field_top + 64), fill=draft_color)
    # Keyboard band starting at y=636 (0.589) with QuickType strip and Return/Send key
    draw.rectangle((0, 636, 499, 1079), fill=(208, 211, 217))
    draw.rectangle((20, 650, 180, 682), fill=(180, 180, 180) if pasted else (225, 225, 225))
    if keyboard_tamper:
        draw.rectangle((220, 720, 260, 765), fill=(18, 132, 255))
    # Bottom-right 发送 button turns from grey to iOS blue once pasted
    send_fill = (10, 132, 255) if (pasted or send_already_blue) else (175, 178, 185)
    draw.rounded_rectangle((386, 922, 490, 988), radius=8, fill=send_fill)
    if extra_edit:
        draw.rectangle((40, 72, 120, 96), fill=(220, 30, 30))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_clipboard_accepts_middleclick_trigger_and_gates_staged_writes():
    async def scenario():
        provider = CompanionClipboard(BASE, KEY, trigger_key="middleclick", protocol="staged-v1")
        assert provider.trigger_key == "middleclick"
        assert provider.trigger_command == "middleclick"

        with pytest.raises(ValueError, match="HIDAGENT_CLIPBOARD_TRIGGER_KEY"):
            CompanionClipboard(BASE, KEY, trigger_key="leftclick", protocol="staged-v1")

        nonce = provider.stage_begin("你好00123")
        # Unauthorized middleclick during active staged paste must be rejected.
        with pytest.raises(DeviceError):
            provider.check_input("middleclick")
        # Authorized trigger write before paste_started is allowed.
        with provider.stage_write(nonce, "middleclick"):
            provider.check_input("middleclick")
        token = provider.pending()["text_url"].rsplit("/", 1)[1]
        assert provider.fetch_payload(token) == {"copy_allowed": True, "text": "你好00123"}
        provider.acknowledge(token, copied=True, focused=False)
        provider.stage_start_paste(nonce)
        # Trigger must never fire again once paste has started.
        with provider.stage_write(nonce, "middleclick"):
            with pytest.raises(DeviceError):
                provider.check_input("middleclick")
        provider.stage_finish(nonce)

    asyncio.run(scenario())


def test_staged_prepare_dispatches_middleclick_trigger_instead_of_key(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        provider.trigger_key = "middleclick"
        middle_calls = []

        async def middleclick(*, before_action=None):
            if before_action is not None:
                before_action()
            provider.check_input("middleclick")
            middle_calls.append("middleclick")

        phone.middleclick = middleclick
        prepared = await manager.prepare(PreparePaste(
            text="你好00123", focused=True, target_box=(.15, .30, .85, .55),
            screen_id=control.observation()["screen_id"],
        ))
        assert prepared["phase"] == "awaiting_copy"
        assert middle_calls == ["middleclick"]
        assert phone.sent == []
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_middle_click_home_is_disabled_when_clipboard_reserves_middleclick(tmp_path):
    provider = CompanionClipboard(BASE, KEY, trigger_key="middleclick", protocol="staged-v1")
    device = SimpleNamespace(
        port_name="/dev/cu.usbserial-test",
        board_status={"hid": {"path": "ble", "middle_click": 1,
                              "ble": {"conn": 1, "bond": 1, "pair": 0, "peer": 1, "pair_seq": 1}}},
    )
    phone = SimpleNamespace(device=device, clipboard=provider)
    profile = tmp_path / "assistive.json"
    at = AssistiveTouch(phone, profile)
    at.set_middle_click_home_verified(True)

    status = at.middle_click_home_status()
    assert status["enabled"] is False
    assert status["reserved_for_clipboard"] is True
    assert at._middle_click_home_enabled() is False

    with pytest.raises(ValueError, match="剪贴板"):
        asyncio.run(at.verify_and_enable_middle_click_home())


def test_paste_flow_skips_assistive_ball_and_pointer_when_trigger_configured(monkeypatch):
    monkeypatch.setattr(assistive_vision, "find_button_target", lambda _frame, near=None: None)
    monkeypatch.setattr(assistive_vision, "track_button_target", lambda _frame, previous=None: None)
    monkeypatch.setattr(paste_flow.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ocr, "read_text", lambda frame: (
        [ocr.TextBox("测试群", (.35, .06, .65, .09), (.50, .075), .99),
         ocr.TextBox("第一行", (.16, .525, .40, .548), (.28, .536), .98),
         ocr.TextBox("第二行", (.16, .552, .40, .575), (.28, .563), .98)]
        if frame == PASTED_JPEG else
        [ocr.TextBox("测试群", (.35, .06, .65, .09), (.50, .075), .99)]
    ))

    class TriggerOnlyClient(FakeClient):
        def capabilities(self):
            return {"features": {"point_measurement": False,
                                 "clipboard": {"trigger_configured": True}}}

    client = TriggerOnlyClient(trigger_configured=True, copied_on_prepare=True, sheet=False, confirm_conflicts=0)
    report = paste_flow.run(client, "第一行\n第二行", WECHAT_FIELD, expect_title="测试群").as_dict()
    assert report["trigger_configured"] is True
    assert report["copy_trigger"] == "trigger_key"
    assert report["cmd_v_sent"] == 1 and report["phase"] == "completed"
    assert "point" not in client.calls
    assert ("click", "辅助触控", .9) not in client.calls


def test_paste_flow_trigger_mode_retries_pre_dispatch_prepare_409_safely(monkeypatch):
    monkeypatch.setattr(assistive_vision, "find_button_target", lambda _frame, near=None: None)
    monkeypatch.setattr(assistive_vision, "track_button_target", lambda _frame, previous=None: None)
    monkeypatch.setattr(paste_flow.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ocr, "read_text", lambda frame: (
        [ocr.TextBox("第一行", (.16, .525, .40, .548), (.28, .536), .98)]
        if frame == PASTED_JPEG else []
    ))

    class FlakyPrepareClient(FakeClient):
        def __init__(self):
            super().__init__(trigger_configured=True, copied_on_prepare=True, sheet=False, confirm_conflicts=0)
            self.prepare_conflicts = 1

        def clipboard_stage(self, stage, **fields):
            if stage == "prepare" and self.prepare_conflicts:
                self.prepare_conflicts -= 1
                self.calls.append("prepare_409")
                raise RuntimeError("HTTP 409: screen changed since observation; observe again")
            return super().clipboard_stage(stage, **fields)

    client = FlakyPrepareClient()
    report = paste_flow.run(client, "第一行", WECHAT_FIELD).as_dict()
    assert client.calls.count("prepare_409") == 1
    assert client.calls.count("prepare") == 1
    assert client.calls.count("paste") == 1
    assert report["retries"].get("prepare") == 1
    assert report["phase"] == "completed" and report["cmd_v_sent"] == 1
    assert "point" not in client.calls


@pytest.mark.parametrize(("grown", "extra_edit", "expected_evidence"), [
    (False, False, "direct_paste_verified"),
    (True, False, "direct_paste_verified"),
    (False, True, None),
    (True, True, None),
])
def test_direct_paste_confirm_without_permission_sheet_in_wechat(monkeypatch, grown, extra_edit, expected_evidence):
    before = _wechat_direct_frame(pasted=False, grown=False)
    after = _wechat_direct_frame(pasted=True, grown=grown, extra_edit=extra_edit)
    shift = 48 / 1080 if grown else 0.0
    text = "第一行测试回复\n第二行测试回复" if grown else "单行测试回复"
    header = [label("测试群(10)", .45, .075)]
    before_chat = [label("买家提问", .25, 290 / 1080), label("历史回复", .65, 410 / 1080)]
    after_chat = [label("买家提问", .25, 290 / 1080 - shift), label("历史回复", .65, 410 / 1080 - shift)]
    keyboard = [label("发送", .88, .885), label("123", .08, .885)]
    if grown:
        field_items = [label("第一行测试回复", .35, (562 - 48 + 20) / 1080),
                       label("第二行测试回复", .35, (562 - 48 + 56) / 1080)]
    else:
        field_items = [label("单行测试回复", .35, (562 + 20) / 1080)]
    items = {
        before: header + before_chat + [label("候选A", .20, .615)] + keyboard,
        after: header + after_chat + field_items + [label("候选B", .20, .615)] + keyboard,
    }

    async def read(frame):
        return items[frame]

    monkeypatch.setattr(ocr, "read_text_async", read)

    result = asyncio.run(paste_confirmation.verify_direct_paste(before, after, WECHAT_FIELD, text))
    assert result == expected_evidence

    if expected_evidence is not None:
        async def end_to_end():
            monkeypatch.setattr("test_staged_clipboard.field_image", lambda text=False: after if text else before)
            screen, provider, phone, control, manager = setup(monkeypatch)
            prepared = await manager.prepare(PreparePaste(
                text=text, focused=True, target_box=WECHAT_FIELD,
                screen_id=control.observation()["screen_id"],
            ))
            tx = prepared["transaction_id"]
            token = provider.pending()["text_url"].rsplit("/", 1)[1]
            assert provider.fetch(token) == text
            provider.acknowledge(token, copied=True, focused=False)
            screen._on_jpeg(before)

            async def effect(_screen, _since):
                screen._on_jpeg(after)
                return after

            monkeypatch.setattr(feedback, "wait_after_action", effect)
            await manager.paste(PasteTransaction(
                transaction_id=tx, focused=True, screen_id=control.observation()["screen_id"],
            ))
            screen._on_jpeg(after)
            receipt = await manager.confirm(ConfirmPaste(
                transaction_id=tx, confirmed=True, observed_text=text,
                screen_id=control.observation()["screen_id"],
            ))
            assert receipt["phase"] == "completed" and receipt["effect_verified"] is True
            assert receipt["confirmation_evidence"] == expected_evidence
            assert phone.sent.count("cmd+v") == 1
            assert provider.current() == {"active": False, "needs_resolution": False}

        asyncio.run(end_to_end())


def test_direct_paste_rejects_unactivated_send_button_when_ungrown(monkeypatch):
    before = _wechat_direct_frame(pasted=False, grown=False, send_already_blue=True)
    after = _wechat_direct_frame(pasted=True, grown=False)
    header = [label("测试群(10)", .45, .075)]
    chat = [label("买家提问", .25, 290 / 1080), label("历史回复", .65, 410 / 1080)]
    keyboard = [label("发送", .88, .885), label("123", .08, .885)]
    items = {
        before: header + chat + [label("候选A", .20, .615)] + keyboard,
        after: header + chat + [label("单行测试回复", .35, (562 + 20) / 1080)] + [label("候选B", .20, .615)] + keyboard,
    }

    async def read(frame):
        return items[frame]

    monkeypatch.setattr(ocr, "read_text_async", read)
    assert asyncio.run(paste_confirmation.verify_direct_paste(before, after, WECHAT_FIELD, "单行测试回复")) is None


@pytest.mark.parametrize(("before_name", "after_name", "text_attr"), [
    ("empty_pinyin", "pasted_three_lines", "TEXT"),
    ("live_original", "live_pasted", "LIVE_TEXT"),
])
def test_direct_paste_confirms_real_wechat_fixtures_without_sheet(monkeypatch, before_name, after_name, text_attr):
    import test_wechat_paste

    before = test_wechat_paste.load(before_name)
    after = test_wechat_paste.load(after_name)
    mapping = {before: test_wechat_paste.boxes(before_name), after: test_wechat_paste.boxes(after_name)}

    async def read(frame):
        return mapping[frame]

    monkeypatch.setattr(ocr, "read_text_async", read)
    assert asyncio.run(
        paste_confirmation.verify_direct_paste(
            before, after, test_wechat_paste.FIELD, getattr(test_wechat_paste, text_attr),
        )
    ) == "direct_paste_verified"


def test_direct_paste_rejects_keyboard_tamper_when_structural_chat_checked(monkeypatch):
    before = _wechat_direct_frame(pasted=False, grown=False)
    after = _wechat_direct_frame(pasted=True, grown=True, keyboard_tamper=True)
    shift = 48 / 1080
    text = "第一行测试回复\n第二行测试回复"
    header = [label("测试群(10)", .45, .075)]
    before_chat = [label("买家提问", .25, 290 / 1080), label("历史回复", .65, 410 / 1080)]
    after_chat = [label("买家提问", .25, 290 / 1080 - shift), label("历史回复", .65, 410 / 1080 - shift)]
    keyboard = [label("发送", .88, .885), label("123", .08, .885)]
    field_items = [label("第一行测试回复", .35, (562 - 48 + 20) / 1080),
                   label("第二行测试回复", .35, (562 - 48 + 56) / 1080)]
    items = {
        before: header + before_chat + [label("候选A", .20, .615)] + keyboard,
        after: header + after_chat + field_items + [label("候选B", .20, .615)] + keyboard,
    }

    async def read(frame):
        return items[frame]

    monkeypatch.setattr(ocr, "read_text_async", read)
    assert asyncio.run(paste_confirmation.verify_direct_paste(before, after, WECHAT_FIELD, text)) is None


@pytest.mark.parametrize("grown", [False, True])
def test_chat_structural_confirm_allows_live_group_message_between_paste_and_confirm(monkeypatch, grown):
    before = _wechat_direct_frame(pasted=False, grown=False)
    after = _wechat_direct_frame(pasted=True, grown=grown, incoming_group_msg=True)
    shift = 48 / 1080 if grown else 0.0
    text = "第一行测试回复\n第二行测试回复" if grown else "单行测试回复"
    header = [label("测试群(10)", .45, .075)]
    before_chat = [label("买家提问", .25, 290 / 1080), label("历史回复", .65, 410 / 1080)]
    after_chat = [
        label("群友刚刚插话", .32, 180 / 1080),
        label("买家提问", .25, 290 / 1080 - shift),
        label("历史回复", .65, 410 / 1080 - shift),
    ]
    keyboard = [label("发送", .88, .885), label("123", .08, .885)]
    if grown:
        field_items = [label("第一行测试回复", .35, (562 - 48 + 20) / 1080),
                       label("第二行测试回复", .35, (562 - 48 + 56) / 1080)]
    else:
        field_items = [label("单行测试回复", .35, (562 + 20) / 1080)]
    items = {
        before: header + before_chat + [label("候选A", .20, .615)] + keyboard,
        after: header + after_chat + field_items + [label("候选B", .20, .615)] + keyboard,
    }

    async def read(frame):
        return items[frame]

    monkeypatch.setattr(ocr, "read_text_async", read)

    async def scenario():
        monkeypatch.setattr("test_staged_clipboard.field_image", lambda text=False: after if text else before)
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(PreparePaste(
            text=text, focused=True, target_box=WECHAT_FIELD,
            screen_id=control.observation()["screen_id"],
        ))
        tx = prepared["transaction_id"]
        token = provider.pending()["text_url"].rsplit("/", 1)[1]
        assert provider.fetch(token) == text
        provider.acknowledge(token, copied=True, focused=False)
        screen._on_jpeg(before)

        async def effect(_screen, _since):
            screen._on_jpeg(after)
            return after

        monkeypatch.setattr(feedback, "wait_after_action", effect)
        await manager.paste(PasteTransaction(
            transaction_id=tx, focused=True, screen_id=control.observation()["screen_id"],
        ))
        screen._on_jpeg(after)
        receipt = await manager.confirm(ConfirmPaste(
            transaction_id=tx, confirmed=True, observed_text=text,
            screen_id=control.observation()["screen_id"],
        ))
        assert receipt["phase"] == "completed" and receipt["effect_verified"] is True
        assert receipt["confirmation_evidence"] == "direct_paste_verified"
        assert phone.sent.count("cmd+v") == 1
        assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["group_message", "header_tamper", "keyboard_tamper", "draft_tamper"])
def test_chat_structural_confirm_allows_group_message_during_ocr_but_rejects_chrome_or_draft_tamper(
    monkeypatch, outcome,
):
    from hidagent import clipboard_control

    monkeypatch.setattr(clipboard_control, "_CONFIRM_SETTLE_TIMEOUT", 0.05)
    before = _wechat_direct_frame(pasted=False, grown=False)
    after = _wechat_direct_frame(pasted=True, grown=True)
    during_ocr = _wechat_direct_frame(
        pasted=True,
        grown=True,
        incoming_group_msg=(outcome == "group_message"),
        header_tamper=(outcome == "header_tamper"),
        keyboard_tamper=(outcome == "keyboard_tamper"),
        draft_tamper=(outcome == "draft_tamper"),
    )
    shift = 48 / 1080
    text = "第一行测试回复\n第二行测试回复"
    header = [label("测试群(10)", .45, .075)]
    before_chat = [label("买家提问", .25, 290 / 1080), label("历史回复", .65, 410 / 1080)]
    after_chat = [label("买家提问", .25, 290 / 1080 - shift), label("历史回复", .65, 410 / 1080 - shift)]
    keyboard = [label("发送", .88, .885), label("123", .08, .885)]
    field_items = [label("第一行测试回复", .35, (562 - 48 + 20) / 1080),
                   label("第二行测试回复", .35, (562 - 48 + 56) / 1080)]
    items = {
        before: header + before_chat + [label("候选A", .20, .615)] + keyboard,
        after: header + after_chat + field_items + [label("候选B", .20, .615)] + keyboard,
    }

    async def scenario():
        monkeypatch.setattr("test_staged_clipboard.field_image", lambda text=False: after if text else before)
        screen, provider, phone, control, manager = setup(monkeypatch)
        mutated = False

        async def read(frame):
            nonlocal mutated
            if frame == after and not mutated:
                mutated = True
                screen._on_jpeg(during_ocr)
            return items[frame]

        monkeypatch.setattr(ocr, "read_text_async", read)
        prepared = await manager.prepare(PreparePaste(
            text=text, focused=True, target_box=WECHAT_FIELD,
            screen_id=control.observation()["screen_id"],
        ))
        tx = prepared["transaction_id"]
        token = provider.pending()["text_url"].rsplit("/", 1)[1]
        assert provider.fetch(token) == text
        provider.acknowledge(token, copied=True, focused=False)
        screen._on_jpeg(before)

        async def effect(_screen, _since):
            screen._on_jpeg(after)
            return after

        monkeypatch.setattr(feedback, "wait_after_action", effect)
        await manager.paste(PasteTransaction(
            transaction_id=tx, focused=True, screen_id=control.observation()["screen_id"],
        ))
        screen._on_jpeg(after)
        req = ConfirmPaste(
            transaction_id=tx, confirmed=True, observed_text=text,
            screen_id=control.observation()["screen_id"],
        )
        if outcome == "group_message":
            receipt = await manager.confirm(req)
            assert receipt["phase"] == "completed" and receipt["effect_verified"] is True
            assert receipt["confirmation_evidence"] == "direct_paste_verified"
        else:
            with pytest.raises(ValueError, match="screen changed since observation"):
                await manager.confirm(req)
        assert phone.sent.count("cmd+v") == 1

    asyncio.run(scenario())


def test_chat_structural_confirm_still_rejects_title_change_and_panel_open(monkeypatch):
    before = _wechat_direct_frame(pasted=False, grown=False)
    after = _wechat_direct_frame(pasted=True, grown=True, incoming_group_msg=True)
    shift = 48 / 1080
    text = "第一行测试回复\n第二行测试回复"
    before_header = [label("测试群(10)", .45, .075)]
    wrong_header = [label("另一个群(5)", .45, .075)]
    before_chat = [label("买家提问", .25, 290 / 1080), label("历史回复", .65, 410 / 1080)]
    after_chat = [label("买家提问", .25, 290 / 1080 - shift), label("历史回复", .65, 410 / 1080 - shift)]
    keyboard = [label("发送", .88, .885), label("123", .08, .885)]
    field_items = [label("第一行测试回复", .35, (562 - 48 + 20) / 1080),
                   label("第二行测试回复", .35, (562 - 48 + 56) / 1080)]
    items = {
        before: before_header + before_chat + [label("候选A", .20, .615)] + keyboard,
        after: wrong_header + after_chat + field_items + [label("候选B", .20, .615)] + keyboard,
    }

    async def read(frame):
        return items[frame]

    monkeypatch.setattr(ocr, "read_text_async", read)
    assert asyncio.run(paste_confirmation.verify_direct_paste(before, after, WECHAT_FIELD, text)) is None

    # Restore matching header, then reject if AssistiveTouch menu panel is open.
    items[after] = before_header + after_chat + field_items + [label("候选B", .20, .615)] + keyboard
    monkeypatch.setattr(assistive_vision, "find_panel", lambda f: (.15, .25, .85, .75) if f == after else None)
    assert asyncio.run(paste_confirmation.verify_direct_paste(before, after, WECHAT_FIELD, text)) is None
