"""Select only a focused Spotlight field supported by same-frame system labels."""

import asyncio
import base64
import io

import pytest
from PIL import Image, ImageDraw
from test_app_search_home import setup

from hidagent import feedback, ocr
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.phone import AppSearchError, TypingVerificationError


def spotlight(*, joined=False, adjacent_full=False, extra=False, caret=True, edit=None):
    image = Image.new("RGB", (500, 1080), (110, 45, 74))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 565, 480, 630), radius=18, fill=(169, 126, 156))
    if caret:
        draw.rectangle((68, 581, 69, 613), fill="white")
    if joined:
        # A complete 33px caret touches a 22px placeholder stroke through JPEG
        # fringe / a thin bridge; the shorter stroke is not another caret core.
        draw.rectangle((73, 581 if adjacent_full else 588, 73, 613 if adjacent_full else 609), fill="white")
        draw.line((69, 600, 75, 600), fill="white")
    if extra:
        draw.rounded_rectangle((130, 920, 370, 975), radius=10, fill=(55, 55, 55))
        draw.rectangle((254, 940, 255, 958), fill="white")
    if edit == "field":
        draw.rectangle((100, 589, 165, 609), fill="white")
    elif edit == "keyboard":
        draw.rectangle((80, 790, 100, 820), fill="white")
    elif edit == "sibling":
        draw.rectangle((250, 420, 280, 440), fill="white")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def system_labels(*, marker=True, placeholder="search", extra=False):
    box = (.16, .54, .29, .56)
    if placeholder == "keyboard":
        box = (.83, .865, .93, .895)  # the keyboard Search key is not its spacebar field
    labels = [ocr.TextBox("搜索" if placeholder != "missing" else "Other", box, (.2, .55), .99)]
    if marker:
        marker_box = (.04, .96, .20, .98) if marker == "below" else (.04, .075, .20, .095)
        labels.append(ocr.TextBox("Siri 建议", marker_box, (.12, .085), .99))
    if extra:
        labels.append(ocr.TextBox("Search", (.31, .865, .42, .89), (.365, .88), .99))
    return labels


@pytest.mark.parametrize("joined,extra", [(True, False), (False, True), (True, True)])
def test_search_uses_unique_label_supported_caret_not_other_white_strokes(tmp_path, monkeypatch, joined, extra):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch, on_home=True)

        async def opened(*_args):
            screen._on_jpeg(spotlight(joined=joined, extra=extra))
            return screen.frame

        async def labels(_frame):
            return system_labels()

        monkeypatch.setattr(feedback, "wait_after_action", opened)
        monkeypatch.setattr(ocr, "read_text_async", labels)
        try:
            region = await phone.open_app_search("Freeform")
            assert region[1] < .54 < .56 < region[3] < .7
            assert link.written == [b"1 key cmd+space\n", b"2 type Freeform\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["missing_marker", "marker_below", "missing_placeholder", "keyboard_placeholder",
                                 "no_caret", "ambiguous_fields", "two_full_cores"])
def test_unrelated_or_ambiguous_fields_cannot_authorize_query(tmp_path, monkeypatch, case):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch, on_home=True)

        async def opened(*_args):
            screen._on_jpeg(spotlight(extra=True, caret=case != "no_caret",
                                     joined=case == "two_full_cores", adjacent_full=case == "two_full_cores"))
            return screen.frame

        async def labels(_frame):
            placeholder = {"missing_placeholder": "missing", "keyboard_placeholder": "keyboard"}.get(case, "search")
            marker = "below" if case == "marker_below" else case != "missing_marker"
            return system_labels(marker=marker, placeholder=placeholder,
                                 extra=case == "ambiguous_fields")

        monkeypatch.setattr(feedback, "wait_after_action", opened)
        monkeypatch.setattr(ocr, "read_text_async", labels)
        try:
            with pytest.raises(AppSearchError):
                await phone.open_app_search("Freeform")
            assert link.written == [b"1 key cmd+space\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", ["field", "keyboard", "sibling"])
def test_post_type_comparison_remains_limited_to_the_proven_field(tmp_path, monkeypatch, edit):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch, on_home=True)
        send = phone._send

        async def opened(*_args):
            screen._on_jpeg(spotlight(joined=True, extra=True))
            return screen.frame

        async def labels(_frame):
            return system_labels()

        async def after_ack(epoch, command, **kwargs):
            result = await send(epoch, command, **kwargs)
            if command.startswith("type "):
                screen._on_jpeg(spotlight(joined=True, extra=True, edit=edit))
            return result

        monkeypatch.setattr(feedback, "wait_after_action", opened)
        monkeypatch.setattr(ocr, "read_text_async", labels)
        monkeypatch.setattr(phone, "_send", after_ack)
        try:
            if edit == "field":
                await phone.open_app_search("Freeform")
            else:
                with pytest.raises(TypingVerificationError, match="screen changed"):
                    await phone.open_app_search("Freeform")
            assert link.written == [b"1 key cmd+space\n", b"2 type Freeform\n"]
        finally:
            device.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["post_layout", "pre_context", "unknown_ack", "negative_ack"])
@pytest.mark.parametrize("batch", [False, True])
def test_search_query_ack_is_separate_from_result_page_verification(tmp_path, monkeypatch, outcome, batch):
    async def scenario():
        phone, device, link, screen = setup(tmp_path, monkeypatch, on_home=True)
        controls = Controls(phone, screen)
        after = spotlight(joined=True, extra=True, edit="sibling")
        send, write, timeout = phone._send, link.write, device._timeout

        async def opened(*_args):
            screen._on_jpeg(spotlight(joined=True, extra=True))
            return screen.frame

        async def labels(_frame):
            if outcome == "pre_context":
                screen._on_jpeg(after)
            return system_labels()

        def query_ack(data):
            if b" type " in data and outcome in {"unknown_ack", "negative_ack"}:
                link.written.append(data)
                if outcome == "negative_ack":
                    link.replies.put_nowait(data.split(b" ", 1)[0] + b" err simulated\n")
            else:
                write(data)

        async def after_ack(epoch, command, **kwargs):
            result = await send(epoch, command, **kwargs)
            if command.startswith("type ") and outcome == "post_layout":
                screen._on_jpeg(after)  # result layout changes before field acceptance
                asyncio.get_running_loop().call_soon(screen._on_jpeg, after)  # fresh post-completion receipt
            return result

        monkeypatch.setattr(feedback, "wait_after_action", opened)
        monkeypatch.setattr(ocr, "read_text_async", labels)
        monkeypatch.setattr(phone, "_send", after_ack)
        monkeypatch.setattr(link, "write", query_ack)
        monkeypatch.setattr(device, "_timeout",
                            lambda command: .02 if command.startswith("type ") else timeout(command))
        try:
            token = controls.observation()["screen_id"]
            request = ControlAction(action="open_app", text="Freeform", screen_id=token)
            followup = ControlAction(action="key", text="a", screen_id=token)
            result = (await controls.perform_batch(token, [request, followup])
                      if batch else await controls.perform(request))
            assert result["error"] and result["effect_verified"] is False
            if outcome == "post_layout":
                assert result["command_acknowledged"] is True
                assert result["requires_verification"] is True and result["input_verified"] is False
                assert result["typing_progress"] == {"chunks_acknowledged": 1, "chunks_total": 1,
                    "characters_acknowledged": 8, "all_chunks_acknowledged": True}
                assert result["fresh"] is True
                assert base64.b64decode(result["image_base64"]) == after
            else:
                assert result["command_acknowledged"] is False
                assert "typing_progress" not in result
            assert len(link.written) == (1 if outcome == "pre_context" else 2)
            assert link.written[0] == b"1 key cmd+space\n"
            if batch:
                assert result["completed"] == 0
                assert len(result["steps"]) == 1 and result["steps"][0]["ok"] is False
                if outcome == "post_layout":
                    assert result["stopped_before_step"] == 2
        finally:
            device.close()

    asyncio.run(scenario())
