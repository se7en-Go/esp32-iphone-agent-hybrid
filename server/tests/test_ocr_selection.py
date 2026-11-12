"""Observed selectors must distinguish controls from duplicate/message text."""

import asyncio
from types import SimpleNamespace

import pytest
from test_controls import FakePhone, Screen, instant_mirror  # noqa: F401

from hidagent import ocr
from hidagent.app import ControlAction
from hidagent.control import Controls


def box(text, bounds, confidence=.99):
    left, top, right, bottom = bounds
    return ocr.TextBox(text, bounds, ((left + right) / 2, (top + bottom) / 2), confidence)


@pytest.fixture
def reader():
    items = []
    calls = []

    def read(frame):
        calls.append(frame)
        return items

    ocr.set_backend(SimpleNamespace(read_text=read))
    yield items, calls
    ocr.set_backend(None)


def perform(items, **kwargs):
    phone = FakePhone()
    controls = Controls(phone, Screen())
    request = ControlAction(action="tap_text", screen_id=controls.observation()["screen_id"], **kwargs)
    return phone, controls, request


def test_duplicate_labels_refuse_without_explicit_selection(reader):
    items, _ = reader
    items.extend([box("设置", (.1, .2, .5, .3)), box("设置", (.1, .6, .5, .7), .7)])
    phone, controls, request = perform(items, text="设置")
    with pytest.raises(ValueError, match="多个|歧义"):
        asyncio.run(controls.perform(request))
    assert phone.calls == []


def test_region_excludes_high_confidence_message_and_reuses_ocr(reader):
    items, calls = reader
    message = box("发送", (.1, .2, .3, .3))
    button = box("发送", (.8, .7, .96, .78), .6)
    items.extend([message, button])
    phone, controls, request = perform(items, text="发送", region=(.75, .65, 1, .85))
    found = asyncio.run(controls.find_text(request.screen_id, "发送", region=request.region))
    result = asyncio.run(controls.perform(request))
    assert found["matches"] == [result["text_match"]]
    assert phone.calls == [("tap", *button.center)]
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["发送", "请点击发送", "Send"])
def test_send_text_in_message_never_authorizes_unscoped_tap(reader, text):
    items, _ = reader
    items.append(box(text, (.1, .2, .5, .3)))
    phone, controls, request = perform(items, text="Send" if text == "Send" else "发送", nth=1)
    with pytest.raises(ValueError, match="region|区域"):
        asyncio.run(controls.perform(request))
    assert phone.calls == []


def test_explicit_index_selects_current_duplicate(reader):
    items, _ = reader
    items.extend([box("设置", (.1, .2, .5, .3)), box("设置", (.1, .6, .5, .7), .7)])
    phone, controls, request = perform(items, text="设置", nth=2)
    asyncio.run(controls.perform(request))
    assert phone.calls == [("tap", *items[1].center)]


def test_region_still_refuses_two_labels_inside_it(reader):
    items, _ = reader
    items.extend([box("设置", (.1, .2, .4, .3)), box("设置", (.6, .2, .9, .3))])
    phone, controls, request = perform(items, text="设置", region=(0, 0, 1, .5))
    with pytest.raises(ValueError, match="歧义"):
        asyncio.run(controls.perform(request))
    assert phone.calls == []


def test_region_does_not_select_partially_overlapping_message(reader):
    items, _ = reader
    items.append(box("请点击发送按钮", (.1, .6, .95, .8)))
    phone, controls, request = perform(items, text="发送", region=(.5, .5, 1, .9))
    with pytest.raises(ValueError, match="未找到"):
        asyncio.run(controls.perform(request))
    assert phone.calls == []


@pytest.mark.parametrize("region", [(0, 0, 2, 1), (.5, .2, .1, .8), (0, float("nan"), 1, 1)])
def test_invalid_region_refuses_before_hid(reader, region):
    items, _ = reader
    items.append(box("设置", (.1, .2, .5, .3)))
    phone, controls, request = perform(items, text="设置", region=region)
    with pytest.raises(ValueError, match="region|区域"):
        asyncio.run(controls.perform(request))
    assert phone.calls == []
