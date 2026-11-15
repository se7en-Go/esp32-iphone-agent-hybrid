"""Explicit host evidence uses the same HTTP gate and actual Device write boundary."""

import asyncio
import base64
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from test_control_input_context import Link
from test_controls import jpeg

from hidagent import app as module
from hidagent import feedback, freshness
from hidagent.control import Controls
from hidagent.device import Device
from hidagent.guard import Guard
from hidagent.phone import Phone
from hidagent.screen import Screen


@pytest.fixture
def rig(tmp_path, monkeypatch):
    now = [datetime(2026, 11, 15, 12).timestamp()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    device, link, screen = Device(), Link(), Screen()
    device.attach(link)
    now[0] += freshness.MIRROR_LAG_SECONDS + .1
    screen._on_jpeg(jpeg())
    phone = Phone(device, tmp_path / "missing-calibration.json", screen)
    phone._clock, phone._clock_link = 0, device.links
    controls = Controls(phone, screen)
    guard = Guard(clock=lambda: now[0], path=tmp_path / "guard.json")
    device.gate = module._hid_gate

    async def effect(*_args):
        now[0] += freshness.MIRROR_LAG_SECONDS + .1
        screen._on_jpeg(jpeg("black"))
        phone._effect_snapshot = freshness.require_fresh(screen)
        phone._effect_frame = screen.frame
        return True, True

    async def no_feedback(*_args, **_kwargs):
        return False

    monkeypatch.setattr(phone, "_tap_effect", effect)
    monkeypatch.setattr(screen, "settled", no_feedback)
    monkeypatch.setattr(feedback, "wait_after_action", no_feedback)
    for name, value in {"device": device, "phone": phone, "screen": screen, "controls": controls,
                        "guard": guard, "agent": SimpleNamespace(running=False),
                        "manual_lock": asyncio.Lock()}.items():
        monkeypatch.setattr(module, name, value)
    yield SimpleNamespace(now=now, device=device, link=link, screen=screen,
                          phone=phone, controls=controls, guard=guard)
    device.close()


def request_for(rig, **changes):
    return {"screen_id": rig.controls.observation()["screen_id"], "pointer_x": .5,
            "pointer_y": .4, "target_box": [.4, .3, .6, .5], "target_label": "主屏幕", **changes}


async def post(body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                 base_url="http://127.0.0.1") as client:
        return await client.post("/api/control/host-click", json=body)


async def passive_observation():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                 base_url="http://127.0.0.1") as client:
        return await client.get("/api/control/observe?for_click=true")


def from_shot(shot):
    return {"screen_id": shot["screen_id"], "pointer_x": .5, "pointer_y": .4,
            "target_box": [.4, .3, .6, .5], "target_label": "主屏幕"}


def test_host_evidence_clicks_once_without_calibration_or_pointer_movement(rig):
    body = request_for(rig)
    before = (rig.phone._pointer_confirmed, rig.phone._pointer_seen)
    result = asyncio.run(post(body))
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["command_acknowledged"] and payload["clicked"]
    assert payload["evidence_source"] == "host_observed"
    assert payload["effect_verified"] is False and payload["pointer_seen"] is False
    assert payload["image_base64"] and payload["screen_id"]
    assert not rig.phone.calibrated
    assert (rig.phone._pointer_confirmed, rig.phone._pointer_seen) == before
    assert [line.split(b" ", 1)[1].strip() for line in rig.link.written] == [b"click"]
    assert rig.guard.sends == []
    assert asyncio.run(post(body)).status_code == 409  # explicit evidence cannot be replayed
    assert len(rig.link.written) == 1


def test_host_click_returns_current_settled_frame_instead_of_first_effect(rig, monkeypatch):
    effect = rig.phone._tap_effect
    final = jpeg("red")

    async def advance_after_effect(*args):
        result = await effect(*args)
        rig.now[0] += .01
        rig.screen._on_jpeg(final)
        return result

    monkeypatch.setattr(rig.phone, "_tap_effect", advance_after_effect)
    result = asyncio.run(post(request_for(rig)))
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["evidence_source"] == "host_observed" and not payload["effect_verified"]
    assert payload["command_acknowledged"] and payload["effect_observed"]
    assert base64.b64decode(payload["image_base64"]) == final
    assert rig.controls.consume(payload["screen_id"])[0] == final
    assert len(rig.link.written) == 1


@pytest.mark.parametrize("changes", [
    {"pointer_x": True}, {"pointer_x": -.01}, {"pointer_y": 1.01},
    {"target_box": [.4, .3, .6]}, {"target_box": [.4, .3, .6, 2]},
    {"target_label": "x" * 121}, {"hold": True}, {"repeat": 2}, {"click_count": 2},
    {"action": "tap"}, {"coordinate_space": "pixels"}, {"screen_id": ""},
])
def test_host_click_schema_rejects_extra_modes_and_invalid_coordinates(rig, changes):
    result = asyncio.run(post(request_for(rig, **changes)))
    assert result.status_code == 422, result.text
    assert rig.link.written == []


@pytest.mark.parametrize("box", [[.5, .3, .6, .5], [.4, .4, .6, .5], [.6, .3, .4, .5]])
def test_host_pointer_must_be_strictly_inside_target(rig, box):
    result = asyncio.run(post(request_for(rig, target_box=box)))
    assert result.status_code == 409 and "strictly inside" in result.text
    assert rig.link.written == []


@pytest.mark.parametrize("transition", ["input", "links", "epoch", "generation", "pixels"])
def test_ticket_is_bound_to_original_input_and_device_session(rig, transition):
    async def scenario():
        body = request_for(rig)
        if transition == "input":
            await rig.device.send("move 1 0")
        elif transition == "links":
            rig.device.links += 1
        elif transition == "epoch":
            rig.device._abort_epoch += 1
        elif transition == "generation":
            rig.screen._on_ready()
            rig.screen._on_jpeg(jpeg())
        else:
            rig.screen._on_jpeg(jpeg("black"))
        result = await post(body)
        assert result.status_code == 409, result.text
        assert not any(b" click" in line for line in rig.link.written)

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["connection", "input"])
def test_image_must_follow_connection_and_last_input_by_full_pipeline(rig, boundary):
    async def scenario():
        if boundary == "connection":
            rig.device.attach(rig.link)
            rig.phone._clock_link = rig.device.links  # model a clock sample for the new serial session
        else:
            await rig.device.send("move 1 0")
        rig.now[0] += 1
        rig.screen._on_jpeg(jpeg())
        denied = await post(request_for(rig))
        assert denied.status_code == 409 and "mirror pipeline" in denied.text
        assert not any(b" click" in line for line in rig.link.written)
        rig.now[0] += 1
        rig.screen._on_jpeg(jpeg())
        accepted = await post(request_for(rig))
        assert accepted.status_code == 200 and accepted.json()["command_acknowledged"]
        assert sum(b" click" in line for line in rig.link.written) == 1

    asyncio.run(scenario())


def test_visible_send_label_uses_existing_quota_without_retry(rig):
    async def scenario():
        first = await post(request_for(rig, target_label="发送"))
        assert first.status_code == 200 and first.json()["command_acknowledged"]
        assert len(rig.guard.sends) == 1
        second = await post(request_for(rig, target_label="Send"))
        assert second.status_code == 409 and "8 秒" in second.text
        assert len(rig.guard.sends) == 1
        assert sum(b" click" in line for line in rig.link.written) == 1

    asyncio.run(scenario())


def test_host_click_retains_clipboard_input_exclusion(rig):
    checked = []

    def reject(command):
        checked.append(command)
        raise RuntimeError("clipboard transaction owns input")

    rig.phone.clipboard = SimpleNamespace(check_input=reject)
    result = asyncio.run(post(request_for(rig)))
    assert result.status_code == 200
    assert not result.json()["command_acknowledged"]
    assert "clipboard" in result.json()["error"]
    assert result.json()["screen_id"] is None
    assert checked == ["click"] and rig.link.written == []


def test_host_click_is_not_a_batch_or_agent_action(rig):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                     base_url="http://127.0.0.1") as client:
            body = request_for(rig)
            step = {"action": "host-click", "x": .5, "y": .4}
            for path, payload in [
                ("/api/control/action", {**step, "screen_id": body["screen_id"]}),
                ("/api/control/batch", {"steps": [step], "screen_id": body["screen_id"]}),
            ]:
                response = await client.post(path, json=payload)
                assert response.status_code == 422
        assert rig.link.written == []

    asyncio.run(scenario())


def test_host_click_keeps_http_deadline_and_no_late_queued_write(rig, monkeypatch):
    from hidagent import operation

    monkeypatch.setattr(operation, "OPERATION_TIMEOUT", .02)

    async def scenario():
        body = request_for(rig)
        await rig.device._lock.acquire()
        try:
            response = await post(body)
            assert response.status_code == 504 and "deadline" in response.text
            assert rig.controls.ticket is None
            assert rig.controls.job is None
        finally:
            rig.device._lock.release()
        await asyncio.sleep(.02)
        assert rig.link.written == [] and rig.device.input_sequence == 0

    asyncio.run(scenario())


def test_host_click_retains_system_guard_and_single_controller(rig):
    async def scenario():
        module.agent.running = True
        busy = await post(request_for(rig))
        assert busy.status_code == 409
        module.agent.running = False
        rig.guard.observe("验证码")
        refused = await post(request_for(rig))
        assert refused.status_code == 409
        assert rig.link.written == []

    asyncio.run(scenario())


def test_capabilities_describes_host_evidence_without_claiming_calibration_or_cv(rig):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                     base_url="http://127.0.0.1") as client:
            payload = (await client.get("/api/control/capabilities")).json()
            feature = payload["features"]["host_observed_click"]
            assert feature["evidence_source"] == "host_observed"
            assert feature["single_click_only"] is True
            assert feature["requires_calibration"] is False
            assert feature["auto_fallback"] is False
            assert "host-click" not in payload["actions"]

    asyncio.run(scenario())


def test_passive_observation_never_calls_pointer_wake_and_qualification_stays_internal(rig, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("passive host observation must not wake or move the pointer")

    monkeypatch.setattr(rig.phone, "ensure_observation", forbidden)
    monkeypatch.setattr(rig.phone, "point_verified", forbidden)
    response = asyncio.run(passive_observation())
    assert response.status_code == 200, response.text
    shot = response.json()
    assert shot["for_click"] is True and shot["observation_mode"] == "passive_host_pointer"
    assert 29 < shot["expires_in_seconds"] <= 30
    assert shot["image_base64"] and shot["screen_id"]
    assert "host_observation" not in shot and "device" not in shot
    assert rig.controls._host_observation is not None
    assert rig.link.written == []


def test_qualified_pointer_can_be_read_for_over_five_seconds_with_current_identical_source(rig):
    async def scenario():
        response = await passive_observation()
        assert response.status_code == 200, response.text
        body = from_shot(response.json())
        rig.now[0] += 6
        rig.screen._on_jpeg(jpeg())  # genuinely received new source, not restamped old snapshot
        clicked = await post(body)
        assert clicked.status_code == 200, clicked.text
        assert clicked.json()["command_acknowledged"]
        assert clicked.json()["evidence_source"] == "host_observed"
        assert [line.split(b" ", 1)[1].strip() for line in rig.link.written] == [b"click"]

    asyncio.run(scenario())


def test_passive_click_waits_for_current_source_without_input(rig):
    async def scenario():
        response = await passive_observation()
        rig.now[0] += 6  # original pixels remain, but current source needs a real new arrival
        task = asyncio.create_task(post(from_shot(response.json())))
        await asyncio.sleep(.02)
        assert not task.done() and rig.link.written == []
        rig.screen._on_jpeg(jpeg())
        clicked = await task
        assert clicked.status_code == 200 and clicked.json()["command_acknowledged"]
        assert len(rig.link.written) == 1

    asyncio.run(scenario())


def test_qualified_click_abandons_changed_frame_instead_of_waiting_for_original_to_return(rig):
    async def scenario():
        response = await passive_observation()
        body = from_shot(response.json())
        rig.now[0] += 6
        task = asyncio.create_task(post(body))
        await asyncio.sleep(.02)
        rig.screen._on_jpeg(jpeg("black"))
        denied = await asyncio.wait_for(task, .5)
        assert denied.status_code == 200 and not denied.json()["command_acknowledged"]
        assert denied.json()["screen_id"] is None
        rig.screen._on_jpeg(jpeg())
        assert (await post(body)).status_code == 409  # A -> B -> A does not revive a consumed ticket
        assert rig.link.written == []

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["links", "epoch", "input", "expired"])
def test_qualified_click_retains_original_session_and_single_use_expiry(rig, transition):
    async def scenario():
        observed = await passive_observation()
        body = from_shot(observed.json())
        if transition == "links":
            rig.device.links += 1
        elif transition == "epoch":
            rig.device._abort_epoch += 1
        elif transition == "input":
            await rig.device.send("move 1 0")
        else:
            ticket = rig.controls.ticket
            rig.controls.ticket = (ticket[0], ticket[1] - 31, *ticket[2:])
        denied = await post(body)
        assert denied.status_code == 409, denied.text
        assert rig.controls.ticket is None
        assert not any(b" click" in line for line in rig.link.written)

    asyncio.run(scenario())


def test_regular_observation_keeps_default_wake_path_and_replaces_qualification(rig, monkeypatch):
    calls = []

    async def ensure():
        calls.append("ensure")
        return freshness.snapshot(rig.screen)

    monkeypatch.setattr(rig.phone, "ensure_observation", ensure)

    async def scenario():
        assert (await passive_observation()).status_code == 200
        previous = rig.controls._host_observation
        assert previous.lease.active
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                     base_url="http://127.0.0.1") as client:
            response = await client.get("/api/control/observe")
            assert response.status_code == 200 and "for_click" not in response.json()
        assert calls == ["ensure"]
        assert rig.controls._host_observation is None
        assert not previous.lease.active

    asyncio.run(scenario())


def test_passive_observation_disconnect_cancels_wait_and_cannot_publish_a_late_ticket(rig, monkeypatch):
    original_observe = rig.phone.observe_host_pointer
    original_acquire = rig.device.acquire_host_observation_lease
    leases = []

    def acquire(*args, **kwargs):
        lease = original_acquire(*args, **kwargs)
        leases.append(lease)
        return lease

    monkeypatch.setattr(rig.device, "acquire_host_observation_lease", acquire)

    async def scenario():
        entered = asyncio.Event()

        async def observing():
            entered.set()
            return await original_observe()

        monkeypatch.setattr(rig.phone, "observe_host_pointer", observing)
        rig.now[0] += 6
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        disconnected = False

        async def receive():
            nonlocal disconnected
            if disconnected:
                return {"type": "http.disconnect"}
            message = await incoming.get()
            disconnected = message["type"] == "http.disconnect"
            return message

        async def send(_message):
            pass

        path = "/api/control/observe"
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                 "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
                 "raw_path": path.encode(), "root_path": "", "query_string": b"for_click=true",
                 "headers": [], "server": ("test", 80), "client": ("test", 1234)}
        task = asyncio.create_task(module.app(scope, receive, send))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            incoming.put_nowait({"type": "http.disconnect"})
            await asyncio.wait_for(task, 1)
            rig.screen._on_jpeg(jpeg())
            await asyncio.sleep(.02)
            assert rig.controls.ticket is None and rig.controls._host_observation is None
            assert rig.controls.job is None and rig.link.written == []
            assert leases and not any(lease.active for lease in leases)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_consumed_host_click_keeps_idle_lease_until_queued_operation_finishes(rig):
    async def scenario():
        observed = await passive_observation()
        qualified = rig.controls._host_observation
        await rig.device._lock.acquire()
        task = asyncio.create_task(post(from_shot(observed.json())))
        try:
            async def waiting():
                while not rig.device._lock._waiters:
                    if task.done():
                        await task
                    await asyncio.sleep(0)

            await asyncio.wait_for(waiting(), 1)
            assert rig.controls.ticket is None and rig.controls._host_observation is None
            assert qualified.lease.active, "consuming a ticket must not release its in-flight idle lease"
            assert rig.link.written == []
            rig.device._lock.release()
            result = await task
            assert result.status_code == 200 and result.json()["command_acknowledged"]
            assert not qualified.lease.active
            assert len(rig.link.written) == 1
        finally:
            if rig.device._lock.locked():
                rig.device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_invalid_host_target_consumes_ticket_and_releases_idle_lease(rig):
    async def scenario():
        observed = await passive_observation()
        qualified = rig.controls._host_observation
        body = {**from_shot(observed.json()), "pointer_x": .9}
        result = await post(body)
        assert result.status_code == 409
        assert rig.controls.ticket is None and not qualified.lease.active
        assert rig.link.written == []

    asyncio.run(scenario())


def test_new_passive_observation_releases_old_idle_lease(rig):
    async def scenario():
        assert (await passive_observation()).status_code == 200
        previous = rig.controls._host_observation
        assert (await passive_observation()).status_code == 200
        current = rig.controls._host_observation
        assert current is not previous and current.lease.active
        assert not previous.lease.active
        rig.controls.invalidate()
        assert not current.lease.active
        assert rig.link.written == []

    asyncio.run(scenario())
