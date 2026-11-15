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
        confirmed = manager.confirm(ConfirmPaste(
            transaction_id=transaction, screen_id=control.observation()["screen_id"],
            observed_text="你好00123", confirmed=True))
        assert confirmed["verified_by"] == "host_observation"
        assert confirmed["server_content_verified"] is False
        assert provider.current() == {"active": False, "needs_resolution": False}
        with pytest.raises((ValueError, RuntimeError, HTTPException)):
            manager.confirm(ConfirmPaste(transaction_id=transaction, screen_id="old",
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
            manager.confirm(ConfirmPaste(transaction_id=prepared["transaction_id"],
                                         screen_id=pasted["screen_id"], observed_text="你好123", confirmed=True))
        assert provider.current()["active"]
        manager.cancel(prepared["transaction_id"])

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


@pytest.mark.parametrize("transition", ["generation", "epoch", "links", "disconnect"])
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
                manager.confirm(ConfirmPaste(transaction_id=nonce,
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
def test_http_stage_deadline_clears_receipts_and_cannot_write_late(monkeypatch, phase):
    from hidagent import operation
    from hidagent.clipboard_control import create_control_router

    async def scenario():
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

        async def slow_key(*args, **kwargs):
            held.append(provider._pending)
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
            result = await client.post("/api/control/clipboard/" + phase, json=body)
            assert result.status_code == 504 and "deadline" in result.text
            await asyncio.sleep(.09)
            assert held and held[0].text is None and held[0].expired
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
            manager.confirm(ConfirmPaste(transaction_id=prepared["transaction_id"],
                screen_id=selected["screen_id"], observed_text="你好00123", confirmed=True))
        assert provider.current()["active"]
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())
