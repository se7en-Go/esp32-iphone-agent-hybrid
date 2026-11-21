"""Staged clipboard contracts; no real shortcuts, network services or phone input."""

import asyncio
import io
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fake_clock import ManualClock
from fastapi import FastAPI, HTTPException
from PIL import Image, ImageDraw

from hidagent.clipboard import CompanionClipboard, create_router
from hidagent.control import Controls
from hidagent.screen import Screen


def field_image(text=False):
    image = Image.new("RGB", (400, 800), "#bbbbbb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 240, 350, 300), fill="white")
    draw.rectangle((90, 252, 92, 278), fill=(0, 100, 255))
    if text:
        draw.rectangle((105, 255, 170, 275), fill="black")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class Phone:
    def __init__(self, screen, clipboard):
        self.screen, self.clipboard = screen, clipboard
        self.device = SimpleNamespace(links=1, abort_epoch=0, connected=True)
        self.sent = []
        self.before_write = None

    async def key(self, combo, *, before_action=None):
        if self.before_write:
            self.before_write()
        if before_action:
            before_action()
        self.clipboard.check_input(f"key {combo}")
        self.sent.append(combo)
        if combo == "cmd+v":
            self.screen._on_jpeg(field_image(text=True))


def setup(monkeypatch):
    from hidagent import feedback, freshness
    from hidagent.clipboard_control import StagedClipboardControls

    monkeypatch.setattr(feedback, "MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 0)
    # Copy ACK and the next simulated frame must have distinct event times,
    # even when the host clock returns the same value for adjacent calls.
    # Keep monotonic time real so HTTP deadline/TTL tests retain their budgets.
    clock = ManualClock()
    monkeypatch.setattr(time, "time", clock.time)
    screen = Screen()

    publish = screen._on_jpeg

    def next_frame(image):
        clock.advance(.01)
        publish(image)

    monkeypatch.setattr(screen, "_on_jpeg", next_frame)
    screen._on_jpeg(field_image())
    provider = CompanionClipboard("http://clipboard.test", "x" * 40, trigger_key="ctrl+alt+p")
    phone = Phone(screen, provider)
    control = Controls(phone, screen)
    manager = StagedClipboardControls(lambda: control)

    async def fresh(*_args):
        screen._on_jpeg(screen.frame)
        return screen.frame

    monkeypatch.setattr(screen, "fresh", fresh)
    return screen, provider, phone, control, manager


def prepare_body(control, **extra):
    from hidagent.clipboard_control import PreparePaste
    return PreparePaste(text="你好00123", screen_id=control.observation()["screen_id"], focused=True, **extra)


def copied(provider, screen=None):
    pending = provider.pending()
    token = pending["text_url"].rsplit("/", 1)[1]
    assert provider.fetch(token) == "你好00123"
    provider.acknowledge(token, copied=True, focused=False)
    if screen is not None:
        screen._on_jpeg(screen.frame)
    return token


def test_prepare_returns_before_copy_and_host_can_observe_then_commit_once(monkeypatch):
    from hidagent.clipboard_control import ConfirmPaste, PasteTransaction

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        result = await manager.prepare(prepare_body(control))
        transaction = result["transaction_id"]
        assert result["phase"] == "awaiting_copy" and not result["effect_verified"]
        assert phone.sent == ["ctrl+alt+p"]
        assert control.observation()["screen_id"]  # no pending Phone.type/complete call blocks the host
        token = copied(provider, control.screen)
        with pytest.raises(HTTPException):
            provider.complete(token, pasted=True)  # Shortcut may not claim host-observed completion
        pasted = await manager.paste(PasteTransaction(
            transaction_id=transaction, screen_id=control.observation()["screen_id"], focused=True))
        assert pasted["phase"] == "awaiting_confirmation" and not pasted["effect_verified"]
        assert pasted["image_base64"] and pasted["screen_id"]
        assert phone.sent == ["ctrl+alt+p", "cmd+v"]
        with pytest.raises((ValueError, RuntimeError, HTTPException)):
            await manager.paste(PasteTransaction(
                transaction_id=transaction, screen_id=pasted["screen_id"], focused=True))
        assert phone.sent.count("cmd+v") == 1
        confirmed = await manager.confirm(ConfirmPaste(
            transaction_id=transaction, screen_id=control.observation()["screen_id"],
            observed_text="你好00123", confirmed=True))
        assert confirmed["verified_by"] == "host_observation"
        assert confirmed["server_content_verified"] is False
        assert provider.current() == {"active": False, "needs_resolution": False}
        with pytest.raises((ValueError, RuntimeError, HTTPException)):
            await manager.confirm(ConfirmPaste(transaction_id=transaction, screen_id="old",
                                         observed_text="你好00123", confirmed=True))

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["generation", "links", "epoch", "screen", "stale"])
def test_original_target_and_session_rechecked_at_actual_write(monkeypatch, transition):
    from hidagent.clipboard_control import PasteTransaction

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, control.screen)
        request = PasteTransaction(transaction_id=prepared["transaction_id"],
                                   screen_id=control.observation()["screen_id"], focused=True)
        reached_write = False

        def change():
            nonlocal reached_write
            reached_write = True
            if transition == "generation":
                screen._on_ready()
                screen._on_jpeg(field_image())
            elif transition == "links":
                phone.device.links += 1
            elif transition == "epoch":
                phone.device.abort_epoch += 1
            elif transition == "screen":
                screen._on_jpeg(field_image(text=True))
            else:
                screen.updated = time.time() - 20
                screen._snapshot = replace(screen.snapshot, updated=screen.updated)

        phone.before_write = change
        result = await manager.paste(request)
        assert reached_write
        assert not result["command_acknowledged"] and result["error"]
        assert "cmd+v" not in phone.sent
        assert provider.current()["needs_resolution"] is True

    asyncio.run(scenario())


def test_staged_transaction_excludes_other_input_and_legacy_paste(monkeypatch):
    async def scenario():
        _, provider, phone, control, manager = setup(monkeypatch)
        await manager.prepare(prepare_body(control))
        for command in ("type overwrite", "key cmd+v", "key a", "key cmd+c"):
            with pytest.raises(RuntimeError):
                provider.check_input(command)
        with pytest.raises(RuntimeError):
            await provider.paste("another", stopped=lambda: False, send=phone.key)
        assert phone.sent == ["ctrl+alt+p"]

    asyncio.run(scenario())


@pytest.mark.parametrize("was_fetched", [False, True])
def test_cancel_invalidates_body_and_nonce_without_resolving_uncertainty(monkeypatch, was_fetched):
    async def scenario():
        _, provider, _, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        pending = provider.pending()
        token = pending["text_url"].rsplit("/", 1)[1]
        if was_fetched:
            provider.fetch(token)
        manager.cancel(prepared["transaction_id"])
        assert provider.current() == {"active": False, "needs_resolution": was_fetched}
        with pytest.raises(HTTPException):
            provider.fetch(token)
        with pytest.raises((HTTPException, ValueError)):
            manager.status(prepared["transaction_id"])

    asyncio.run(scenario())


def test_missing_focus_requires_explicit_observed_target_box(monkeypatch):
    async def scenario():
        screen, _, _, control, manager = setup(monkeypatch)
        image = Image.new("RGB", (400, 800), "white")
        output = io.BytesIO()
        image.save(output, "PNG")
        screen._on_jpeg(output.getvalue())
        with pytest.raises(ValueError, match="target_box"):
            await manager.prepare(prepare_body(control))
        result = await manager.prepare(prepare_body(control, target_box=(.1, .2, .8, .5)))
        assert result["focus_verified_by"] == "host_observation"
        manager.cancel(result["transaction_id"])

    asyncio.run(scenario())


def test_host_confirmation_requires_exact_text_and_post_paste_frame(monkeypatch):
    from hidagent.clipboard_control import ConfirmPaste, PasteTransaction

    async def scenario():
        _, provider, _, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, control.screen)
        pasted = await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                                     screen_id=control.observation()["screen_id"], focused=True))
        with pytest.raises(ValueError, match="exact"):
            await manager.confirm(ConfirmPaste(transaction_id=prepared["transaction_id"],
                                         screen_id=pasted["screen_id"], observed_text="你好123", confirmed=True))
        assert provider.current()["active"]
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["accepted", "unproved", "new_page", "new_session", "new_device", "new_screen"])
def test_permission_confirmation_keeps_the_original_transaction_and_current_ticket(monkeypatch, transition):
    from hidagent import feedback, paste_confirmation
    from hidagent.clipboard_control import ConfirmPaste, PasteTransaction

    def changed_scene(color):
        with Image.open(io.BytesIO(field_image(text=True))) as image:
            ImageDraw.Draw(image).rectangle((50, 80, 350, 155), fill=color)
            output = io.BytesIO()
            image.save(output, "PNG")
            return output.getvalue()

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        before, sheet, after = field_image(), changed_scene("blue"), changed_scene("orange")

        async def capture(_screen, _completed_at):
            screen._on_jpeg(sheet)
            return sheet

        calls = []

        async def verify(original, presented, selected, region, text):
            assert (original, selected, text) == (before, after, "你好00123")
            assert region == manager.transaction.guard.region
            if not calls:
                assert presented == sheet
            calls.append(presented)
            if transition == "new_page":
                screen._on_jpeg(changed_scene("green"))
            if transition == "new_session":
                phone.device.links += 1
            if transition == "new_device":
                phone.device = SimpleNamespace(**vars(phone.device))
            if transition == "new_screen":
                replacement = Screen()
                replacement._on_jpeg(after)
                assert replacement.generation == screen.generation
                phone.screen = control.screen = replacement
            return presented == sheet and transition != "unproved"

        monkeypatch.setattr(feedback, "wait_after_action", capture)
        monkeypatch.setattr(paste_confirmation, "verify_after_paste_permission", verify)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        screen._on_jpeg(after)
        request = ConfirmPaste(transaction_id=prepared["transaction_id"],
                               screen_id=control.observation()["screen_id"],
                               observed_text="你好00123", confirmed=True)
        if transition == "accepted":
            receipt = await manager.confirm(request)
            assert receipt["phase"] == "completed" and receipt["effect_verified"]
            assert receipt["confirmation_evidence"] == "paste_permission_dismissed"
            assert receipt["verified_by"] == "host_observation" and not receipt["server_content_verified"]
            assert provider.current() == {"active": False, "needs_resolution": False}
        else:
            with pytest.raises((ValueError, RuntimeError, HTTPException)):
                await manager.confirm(request)
            if transition in {"new_session", "new_device", "new_screen"}:
                assert provider.current() == {"active": False, "needs_resolution": True}
            else:
                assert provider.current()["active"]
        assert phone.sent == ["ctrl+alt+p", "cmd+v"]

    asyncio.run(scenario())


def test_router_releases_shared_lock_after_prepare(monkeypatch):
    from hidagent.clipboard_control import create_control_router

    async def scenario():
        _, provider, _, control, _ = setup(monkeypatch)
        lock = asyncio.Lock()

        def available():
            if lock.locked():
                raise HTTPException(409, "busy")

        async def connected(_request, awaitable):
            return await awaitable

        app = FastAPI()
        app.include_router(create_router(lambda: provider))
        app.include_router(create_control_router(lambda: control, lambda: lock, available,
                                                 lambda: None, connected))

        @app.get("/observe")
        async def observe():
            available()
            async with lock:
                return control.observation()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/api/control/clipboard/prepare", json=prepare_body(control).model_dump())
            assert result.status_code == 200
            assert not lock.locked()
            assert (await client.get("/observe")).status_code == 200
            assert (await client.post("/api/control/clipboard/cancel", json={
                "transaction_id": result.json()["transaction_id"]})).status_code == 200

    asyncio.run(scenario())


def test_expiry_at_write_never_pastes_and_late_receipt_cannot_revive_transaction(monkeypatch):
    from hidagent.clipboard_control import PasteTransaction

    async def scenario():
        _, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        token = copied(provider, control.screen)
        item = provider._pending
        phone.before_write = lambda: provider._expire(item)
        result = await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                                     screen_id=control.observation()["screen_id"], focused=True))
        assert result["command_acknowledged"] is False and "cmd+v" not in phone.sent
        assert item.text is None and provider.current()["needs_resolution"] is True
        with pytest.raises(HTTPException):
            provider.acknowledge(token, copied=True, focused=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepare", "paste"])
def test_cancelled_stage_cleans_pending_text_and_input_reservation(monkeypatch, phase):
    from hidagent.clipboard_control import PasteTransaction

    async def scenario():
        _, provider, phone, control, manager = setup(monkeypatch)
        entered = asyncio.Event()

        async def hang(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        if phase == "prepare":
            phone.key = hang
            operation = manager.prepare(prepare_body(control))
        else:
            prepared = await manager.prepare(prepare_body(control))
            copied(provider, control.screen)
            phone.key = hang
            operation = manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                                       screen_id=control.observation()["screen_id"], focused=True))
        task = asyncio.create_task(operation)
        await entered.wait()
        pending = provider._pending
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pending.text is None and provider._pending is None
        assert provider._operation_active is False
        assert provider.current()["needs_resolution"] is (phase == "paste")

    asyncio.run(scenario())


def test_structured_payload_requires_active_staged_request_and_is_one_shot(monkeypatch):
    async def scenario():
        _, provider, _, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        app = FastAPI()
        app.include_router(create_router(lambda: provider))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://clipboard.test") as client:
            pending = provider.pending()
            response = await client.get(pending["payload_url"])
            assert response.status_code == 200
            assert response.json() == {"copy_allowed": True, "text": "你好00123"}
            again = await client.get(pending["payload_url"])
            assert again.status_code == 404 and "copy_allowed" not in again.json()
            receipt = await client.post(pending["ack_url"], json={"copied": True})
            assert receipt.status_code == 200
            assert provider.stage_status(prepared["transaction_id"])["copied"] is True
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["generation", "epoch", "links", "disconnect", "screen_stopped"])
@pytest.mark.parametrize("fetched", [False, True])
def test_session_change_revokes_all_receipts_before_any_late_companion_access(monkeypatch, transition, fetched):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        pending = provider.pending()
        item = provider._pending
        token = item.token
        if fetched:
            provider.fetch_payload(token)
        if transition == "generation":
            screen._on_ready()
            screen._on_jpeg(field_image())
        elif transition == "epoch":
            phone.device.abort_epoch += 1
        elif transition == "links":
            phone.device.links += 1
        elif transition == "screen_stopped":
            screen.stop()
        else:
            phone.device.connected = False

        app = FastAPI()
        app.include_router(create_router(lambda: provider))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://clipboard.test") as client:
            response = await client.get(pending["payload_url"])
            assert response.status_code == 404 and "copy_allowed" not in response.json()
            assert (await client.post(pending["ack_url"], json={"copied": True})).status_code == 404
        assert item.text is None and item.expired
        assert provider.current() == {"active": False, "needs_resolution": fetched}
        assert provider._operation_active is False
        provider.check_input("type new input")  # stopped transaction must not retain a keyboard reservation
        with pytest.raises(HTTPException):
            manager.status(prepared["transaction_id"])
        assert manager.transaction is None

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["board", "ipc"])
@pytest.mark.parametrize("already_copied", [False, True])
def test_live_idle_source_preserves_copy_status_without_authorizing_old_pixels(
        monkeypatch, backend, already_copied):
    from hidagent.screen_uxplay import UxPlayScreen

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        if backend == "ipc":
            metadata = {"instance": "test-receiver", "generation": 1, "running": True, "live": True,
                        "updated": screen.updated, "arrived": time.monotonic()}
            image = screen.frame
            screen = UxPlayScreen(socket_path="unused-test-socket")
            screen._accept_remote(metadata, image)
            phone.screen = control.screen = screen
        prepared = await manager.prepare(prepare_body(control))
        item = provider._pending
        if already_copied:
            copied(provider)
        original = screen.snapshot
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 6)
        if backend == "ipc":
            # IPC heartbeat advances, but the receiver republishes unchanged
            # source clocks. It must not freshen pixels just to retain a copy.
            screen._accept_remote(metadata, image)
        state = manager.status(prepared["transaction_id"])
        assert state["phase"] == ("awaiting_paste" if already_copied else "awaiting_copy")
        assert state["fresh"] is False and state["screen_id"] is None and state["image_base64"] is None
        assert provider._pending is item and not item.expired
        assert item.deadline > time.monotonic()
        assert screen.snapshot.updated == original.updated
        assert phone.sent == ["ctrl+alt+p"]
        if not already_copied:
            assert provider.fetch_payload(item.token)["copy_allowed"] is True
            provider.acknowledge(item.token, copied=True, focused=False)
        assert provider.stage_status(item.host_nonce)["copied"] is True
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepare", "paste", "confirm"])
def test_idle_transaction_does_not_authorize_a_stale_input_stage(monkeypatch, phase):
    from hidagent.clipboard_control import ConfirmPaste, PasteTransaction

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        request = prepare_body(control)
        if phase != "prepare":
            prepared = await manager.prepare(request)
            copied(provider, screen)
            request = PasteTransaction(transaction_id=prepared["transaction_id"],
                                       screen_id=control.observation()["screen_id"], focused=True)
            if phase == "confirm":
                await manager.paste(request)
                request = ConfirmPaste(transaction_id=prepared["transaction_id"],
                                       screen_id=control.observation()["screen_id"],
                                       observed_text="你好00123", confirmed=True)
        sent = list(phone.sent)
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 6)
        with pytest.raises((ValueError, RuntimeError), match="新鲜"):
            await getattr(manager, phase)(request)
        assert phone.sent == sent
        if phase == "prepare":
            assert provider._pending is None and manager.transaction is None
        else:
            assert provider.current()["active"] and manager.transaction is not None
            manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["ipc_disconnected", "ipc_heartbeat_lost", "stopped", "generation"])
def test_idle_ipc_session_loss_revokes_pending_copy(monkeypatch, transition):
    from hidagent.screen_uxplay import UxPlayScreen

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        metadata = {"instance": "test-receiver", "generation": 1, "running": True, "live": True,
                    "updated": screen.updated, "arrived": time.monotonic()}
        image = screen.frame
        screen = UxPlayScreen(socket_path="unused-test-socket")
        screen._accept_remote(metadata, image)
        phone.screen = control.screen = screen
        prepared = await manager.prepare(prepare_body(control))
        item = provider._pending
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 6)
        if transition == "ipc_disconnected":
            screen._ipc_connected = False
        elif transition == "ipc_heartbeat_lost":
            screen._ipc_received = time.monotonic() - 3
        elif transition == "stopped":
            screen._stopping = True
        else:
            screen._accept_remote({**metadata, "generation": 2}, image)
        with pytest.raises(HTTPException) as rejected:
            manager.status(prepared["transaction_id"])
        assert rejected.value.status_code == 404
        assert item.expired and item.text is None
        assert provider._pending is None and manager.transaction is None
        assert phone.sent == ["ctrl+alt+p"]

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["status", "paste", "confirm"])
def test_host_access_after_abort_cleans_transaction_without_waiting_for_ttl(monkeypatch, operation):
    from hidagent.clipboard_control import ConfirmPaste, PasteTransaction

    async def scenario():
        _, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        nonce = prepared["transaction_id"]
        copied(provider, control.screen)
        phone.device.abort_epoch += 1
        with pytest.raises((HTTPException, RuntimeError)):
            if operation == "status":
                manager.status(nonce)
            elif operation == "paste":
                await manager.paste(PasteTransaction(transaction_id=nonce,
                    screen_id=control.observation()["screen_id"], focused=True))
            else:
                await manager.confirm(ConfirmPaste(transaction_id=nonce,
                    screen_id=control.observation()["screen_id"], observed_text="你好00123", confirmed=True))
        assert provider.current() == {"active": False, "needs_resolution": True}
        assert manager.transaction is None and "cmd+v" not in phone.sent

    asyncio.run(scenario())


def test_explicit_stop_immediately_purges_unfetched_text(monkeypatch):
    async def scenario():
        _, provider, _, control, manager = setup(monkeypatch)
        await manager.prepare(prepare_body(control))
        item = provider._pending
        provider.cancel_staged()
        assert item.text is None and item.expired
        assert provider.current() == {"active": False, "needs_resolution": False}
        assert provider._operation_active is False
        with pytest.raises(HTTPException):
            provider.fetch_payload(item.token)

    asyncio.run(scenario())


@pytest.mark.parametrize("observation", ["before_copy", "pipeline", "fresh"])
def test_paste_requires_observed_frame_after_copy_and_mirror_pipeline(monkeypatch, observation):
    from hidagent import freshness
    from hidagent.clipboard_control import PasteTransaction

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        pre_copy = prepared["screen_id"]
        copied(provider)
        if observation == "before_copy":
            screen._on_jpeg(field_image())  # current may be new while selected ticket still predates ACK
            token = pre_copy
        else:
            screen._on_jpeg(field_image())
            token = control.observation()["screen_id"]
            if observation == "pipeline":
                monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 1.7)
        request = PasteTransaction(transaction_id=prepared["transaction_id"], screen_id=token, focused=True)
        if observation == "fresh":
            assert (await manager.paste(request))["command_acknowledged"]
            assert phone.sent.count("cmd+v") == 1
        else:
            with pytest.raises((ValueError, RuntimeError), match="cop|复制"):
                await manager.paste(request)
            assert "cmd+v" not in phone.sent
            assert provider.current()["active"]
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepare", "paste"])
@pytest.mark.parametrize("clock_resolution", [None, .02], ids=["host-clock", "coarse-clock"])
def test_http_stage_deadline_clears_receipts_and_cannot_write_late(monkeypatch, phase, clock_resolution):
    from hidagent import operation
    from hidagent.clipboard_control import create_control_router

    async def scenario():
        loop = asyncio.get_running_loop()
        if clock_resolution is not None:
            loop._clock_resolution = clock_resolution
        _, provider, phone, control, _ = setup(monkeypatch)
        lock = asyncio.Lock()

        async def connected(_request, awaitable):
            return await awaitable

        app = FastAPI()
        app.include_router(create_router(lambda: provider))
        app.include_router(create_control_router(lambda: control, lambda: lock,
                                                 lambda: None, lambda: None, connected))
        app.add_middleware(operation.ActionLifetimeMiddleware)
        held = []
        original_key = phone.key
        deadline_clock = ManualClock()
        deadline_timers = []
        call_later = loop.call_later

        def schedule_timer(delay, callback, *args, context=None):
            if callback.__module__ == operation.__name__ and callback.__name__ == "expire":
                assert delay == .01
                # Keep the real middleware expiry callback, but dispatch it at
                # the exact tested phase rather than racing ASGI setup against
                # a 10ms timer (less than one Windows monotonic clock tick).
                handle = asyncio.TimerHandle(loop.time() + delay, callback, args, loop, context=context)
                deadline_timers.append(handle)
                return handle
            return call_later(delay, callback, *args, context=context)

        async def slow_key(*args, **kwargs):
            held.append(provider._pending)
            assert len(deadline_timers) == 1
            deadline_clock.advance(.01)
            assert deadline_clock.monotonic() == operation._current.get().deadline
            loop.call_soon(deadline_timers[0]._run)
            await asyncio.sleep(.08)
            await original_key(*args, **kwargs)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            if phase == "prepare":
                body = prepare_body(control).model_dump()
            else:
                prepared = await client.post("/api/control/clipboard/prepare", json=prepare_body(control).model_dump())
                copied(provider, control.screen)
                body = {"transaction_id": prepared.json()["transaction_id"],
                        "screen_id": control.observation()["screen_id"], "focused": True}
            phone.key = slow_key
            monkeypatch.setattr(operation, "OPERATION_TIMEOUT", .01)
            monkeypatch.setattr(operation, "time", deadline_clock)
            monkeypatch.setattr(loop, "call_later", schedule_timer)
            result = await asyncio.wait_for(client.post("/api/control/clipboard/" + phase, json=body), 1)
            assert result.status_code == 504 and "deadline" in result.text
            await asyncio.sleep(.09)
            assert held and held[0].text is None and held[0].expired
            assert deadline_timers[0].cancelled()
            assert provider._operation_active is False
            assert provider.current() == {"active": False, "needs_resolution": phase == "paste"}
            assert "cmd+v" not in phone.sent
            assert phone.sent == ([] if phase == "prepare" else ["ctrl+alt+p"])
            assert control.job is None and not lock.locked()
            assert (await client.get(f"/api/clipboard/{held[0].token}/payload")).status_code == 404

    asyncio.run(scenario())


def test_confirmation_cannot_use_frame_still_in_post_paste_pipeline(monkeypatch):
    from hidagent import freshness
    from hidagent.clipboard_control import ConfirmPaste, PasteTransaction

    async def scenario():
        screen, provider, _, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        copied(provider, screen)
        await manager.paste(PasteTransaction(transaction_id=prepared["transaction_id"],
                                             screen_id=control.observation()["screen_id"], focused=True))
        manager.transaction.completed_at = time.time() - 2
        early = replace(screen.snapshot, updated=manager.transaction.completed_at + .1)
        selected = control.observation(accepted=early)
        screen._on_jpeg(screen.frame)
        monkeypatch.setattr(freshness, "MIRROR_LAG_SECONDS", 1.7)
        with pytest.raises(ValueError, match="post-paste"):
            await manager.confirm(ConfirmPaste(transaction_id=prepared["transaction_id"],
                screen_id=selected["screen_id"], observed_text="你好00123", confirmed=True))
        assert provider.current()["active"]
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_pre_dispatch_paste_rejection_sends_no_cmd_v_and_keeps_transaction_retryable(monkeypatch):
    """paste_flow retries a paste-stage 409; that is only safe if the 409 precedes Cmd+V."""
    from hidagent.clipboard_control import PasteTransaction

    def bannered():
        with Image.open(io.BytesIO(field_image())) as image:
            ImageDraw.Draw(image).rectangle((20, 10, 380, 70), fill=(40, 40, 40))  # a transient top banner
            output = io.BytesIO()
            image.save(output, "PNG")
            return output.getvalue()

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        transaction = prepared["transaction_id"]
        copied(provider, screen)
        screen._on_jpeg(bannered())
        with pytest.raises((ValueError, RuntimeError)):
            await manager.paste(PasteTransaction(
                transaction_id=transaction, screen_id=control.observation()["screen_id"], focused=True))
        assert "cmd+v" not in phone.sent
        state = manager.status(transaction, include_observation=False)
        assert state["copied"] and not state["paste_sent"] and state["phase"] == "awaiting_paste"
        screen._on_jpeg(field_image())  # the banner left; the original context is back
        pasted = await manager.paste(PasteTransaction(
            transaction_id=transaction, screen_id=control.observation()["screen_id"], focused=True))
        assert pasted["command_acknowledged"] is True and phone.sent.count("cmd+v") == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("tamper_title", [False, True])
def test_staged_paste_allows_stationary_assistive_opacity_change_but_blocks_context_edits(monkeypatch, tamper_title):
    from pathlib import Path

    from hidagent.clipboard_control import PasteTransaction, PreparePaste

    fixtures = Path(__file__).parent / "fixtures" / "wechat_paste"
    dim = (fixtures / "assistive_dim.jpg").read_bytes()
    bright = (fixtures / "assistive_bright.jpg").read_bytes()
    if tamper_title:
        with Image.open(io.BytesIO(bright)) as image:
            rgb = image.convert("RGB")
            ImageDraw.Draw(rgb).rectangle((180, 70, 320, 95), fill=(200, 200, 200))
            out = io.BytesIO()
            rgb.save(out, "JPEG", quality=92)
            bright = out.getvalue()

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        screen._on_jpeg(dim)
        prepared = await manager.prepare(PreparePaste(
            screen_id=control.observation()["screen_id"],
            text="你好00123",
            focused=True,
            target_box=[.132, .523, .768, .578],
        ))
        transaction = prepared["transaction_id"]
        copied(provider, screen)
        screen._on_jpeg(bright)
        if tamper_title:
            with pytest.raises(RuntimeError, match="screen changed"):
                await manager.paste(PasteTransaction(
                    transaction_id=transaction, screen_id=control.observation()["screen_id"], focused=True))
            assert "cmd+v" not in phone.sent
        else:
            pasted = await manager.paste(PasteTransaction(
                transaction_id=transaction, screen_id=control.observation()["screen_id"], focused=True))
            assert pasted["command_acknowledged"] is True and phone.sent.count("cmd+v") == 1

    asyncio.run(scenario())


def test_stage_begin_without_running_loop_leaves_clipboard_unblocked():
    from hidagent.clipboard import CompanionClipboard

    provider = CompanionClipboard("http://example.invalid:8891", "k" * 40, ttl=60)
    with pytest.raises(RuntimeError):
        provider.stage_begin("你好")
    assert provider._pending is None
    assert provider._operation_active is False


def test_abandon_suppresses_unexpected_finish_error_without_masking_cancellation(monkeypatch):
    from fastapi import HTTPException

    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        transaction = prepared["transaction_id"]
        copied(provider, screen)

        async def cancel_on_key(*_args, **_kwargs):
            raise asyncio.CancelledError

        def broken_finish(*_args, **_kwargs):
            raise HTTPException(500, "unexpected finish boom")

        monkeypatch.setattr(phone, "key", cancel_on_key)
        monkeypatch.setattr(provider, "stage_finish", broken_finish)
        from hidagent.clipboard_control import PasteTransaction
        with pytest.raises(asyncio.CancelledError):
            await manager.paste(PasteTransaction(
                transaction_id=transaction, screen_id=control.observation()["screen_id"], focused=True))
        assert manager.transaction is None

    asyncio.run(scenario())
