import asyncio

import pytest
from input_scene import input_frame
from test_agent_context import Model
from test_agent_text import setup
from test_assistive_home import frame as home_frame
from test_control_input_context import Link
from test_controls import jpeg

from hidagent import feedback, freshness, ocr
from hidagent.device import Device
from hidagent.phone import AppSearchError, Phone, PointerLandingError
from hidagent.screen import Screen


def test_pointer_landing_failure_does_not_retry_same_coordinate_or_replan():
    agent, phone, _ = setup()
    agent.actor = Model(["DO: tap 150 260", "DO: tap 151 260"])
    agent.planner = Model(["1. Tap the target"])

    async def tap(*point, **_kwargs):
        phone.calls.append(point)
        raise PointerLandingError("No click was sent")

    phone.tap_verified = tap
    asyncio.run(agent._run())

    assert phone.calls == [(.15, .26)]
    assert len(agent.planner.prompts) == 1
    assert "pointer landing" in agent.actor.prompts[-1]
    assert agent.steps[-1]["action"] == "fail"
    assert agent.steps[-1]["phase"] == "pointer_landing"


@pytest.mark.parametrize("following", [
    "DO: key enter",
    'DO: tap_text {"text":"Freeform","region":[0.05,0.65,0.9,0.86],"exact":true}',
    'DO: tap_text {"text":"无边记","region":[0.05,0.65,0.9,0.86],"exact":true}',
    'DO: tap_text {"text":"备忘录","region":[0.05,0.12,0.5,0.4],"exact":true}',
    'DO: tap_text {"text":"无边记","region":[0.05,0.12,0.5,0.4],"exact":false}',
])
def test_agent_rejects_unverified_app_selection_after_open(following):
    agent, phone, _ = setup()
    phone.app_search_available = True
    agent.actor = Model(['DO: open "Freeform"', following])
    agent.planner = Model(["1. Search for Freeform"])

    async def open_search(name, *, before_action):
        before_action()
        phone.calls.append(("open", name))
        return (.1, .7, .85, .8)

    phone.open_app_search = open_search
    asyncio.run(agent._run())

    assert phone.calls == [("open", "Freeform")]
    assert len(agent.planner.prompts) == 1
    assert agent.steps[-1]["action"] == "fail"
    assert agent.steps[-1]["phase"] == "app_search"


@pytest.mark.parametrize("label", ["Freeform", "无边记", "無邊記"])
def test_agent_selects_only_an_observed_app_result(monkeypatch, label):
    agent, phone, screen = setup()
    phone.app_search_available = True
    agent.actor = Model([
        "DO: open Freeform",
        f'DO: tap_text {{"text":"{label}","region":[0.05,0.12,0.5,0.4],"exact":true}}',
        "DO: done The app is open",
    ])
    agent.planner = Model(["1. Search for Freeform"])
    field, result = (.1, .7, .85, .8), (.12, .15, .24, .18)

    async def open_search(_name, *, before_action):
        before_action()
        return field

    async def find(_frame, query, exact, *, region):
        assert exact is True
        if region == field:
            assert query == "Freeform"
            return [ocr.TextBox("Freeform", (.2, .74, .32, .78), (.26, .76), .98)]
        assert query == label
        return [ocr.TextBox(label, result, (.18, .165), .99)]

    async def tap(*point, before_click, target_box, target_frame):
        before_click()
        assert target_box == result and target_frame == screen.frame
        phone.calls.append(point)
        return {"clicked": True, "fresh": True, "screen_changed": True, "_after_frame": jpeg("black")}

    phone.open_app_search, phone.tap_verified = open_search, tap
    monkeypatch.setattr(ocr, "find_matches_async", find)
    asyncio.run(agent._run())

    assert phone.calls == [(.18, .165)]
    assert agent.steps[-1]["action"] == "done"
    assert "无边记" in agent.actor.prompts[1]


@pytest.mark.parametrize("mode", ["disabled", "unfocused", "other_search", "ready"])
def test_phone_opens_and_types_only_after_observing_spotlight(tmp_path, monkeypatch, mode):
    if mode == "disabled":
        monkeypatch.delenv("HIDAGENT_SPOTLIGHT_BLE_VERIFIED", raising=False)
    else:
        monkeypatch.setenv("HIDAGENT_SPOTLIGHT_BLE_VERIFIED", "1")
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)

    async def scenario():
        device, link, screen = Device(), Link(), Screen()
        device.attach(link)
        device.board_status = {"hid": {"path": "ble"}}
        screen._on_jpeg(home_frame(home=True))
        phone = Phone(device, tmp_path / "calibration.json", screen)

        async def wait_after_action(_screen, _when):
            await asyncio.sleep(0)
            screen._on_jpeg(input_frame(focused=mode in {"other_search", "ready"}))
            return screen.frame

        async def read_labels(_frame):
            return [ocr.TextBox("Search", (.15, .765, .27, .795), (.21, .78), .99),
                    ocr.TextBox("Other App" if mode == "other_search" else "Siri Suggestions",
                                (.1, .3, .35, .34), (.22, .32), .98)]

        monkeypatch.setattr(feedback, "wait_after_action", wait_after_action)
        monkeypatch.setattr(ocr, "read_text_async", read_labels)
        try:
            if mode == "ready":
                field = await phone.open_app_search("Freeform")
                assert field[0] < .15 < .27 < field[2]
                assert link.written == [b"1 key cmd+space\n", b"2 type Freeform\n"]
            else:
                with pytest.raises(AppSearchError):
                    await phone.open_app_search("Freeform")
                assert link.written == ([] if mode == "disabled" else [b"1 key cmd+space\n"])
        finally:
            device.close()

    asyncio.run(scenario())
