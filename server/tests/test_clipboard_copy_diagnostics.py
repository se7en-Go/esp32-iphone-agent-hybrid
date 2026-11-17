"""Copy-failure diagnostics: tell the three Shortcut failure cases apart.

There is no request log to consult (both uvicorn listeners run with
access_log=False and the clipboard proxy forwards without logging), and
paste-status used to expose only `copied`. These tests pin the counters that
now separate, from the host side alone:

  1. the Shortcut never reached the server (all counters zero),
  2. it reached /pending but never claimed the payload,
  3. it claimed the payload but never acknowledged the copy.

All diagnostics are counts and timestamps; no pasted text, token, URL or
pairing key is ever recorded. Offline only: fake device, ASGI transport.
"""

import asyncio

import httpx
from fastapi import FastAPI
from test_staged_clipboard import prepare_body, setup

from hidagent.clipboard import create_router

KEY = "x" * 40
PAIRED = {"Authorization": f"Bearer {KEY}"}
WRONG = {"Authorization": "Bearer " + "y" * 40}


def client_for(provider):
    app = FastAPI()
    app.include_router(create_router(lambda: provider))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://clipboard.test")


async def pending_item(client):
    response = await client.get("/api/clipboard/pending", headers=PAIRED)
    assert response.status_code == 200
    return response.json()


def test_fresh_staged_status_is_unclaimed_with_zero_counters(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        status = provider.stage_status(prepared["transaction_id"])
        assert status["phase"] == "awaiting_copy"
        assert status["copied"] is False
        assert status["claimed"] is False
        assert status["claimed_at"] is None
        assert status["pending_polls"] == 0
        assert status["claim_attempts"] == 0
        assert status["ack_attempts"] == 0
        assert status["last_ack_error"] is None
        assert status["pending_auth_failures"] == 0
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_pending_poll_without_claim_records_the_visit(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        async with client_for(provider) as client:
            first = await pending_item(client)
            assert first["pending"] is True
            assert "payload_url" in first
            second = await pending_item(client)
            assert second["pending"] is True
        status = provider.stage_status(prepared["transaction_id"])
        assert status["pending_polls"] == 2
        assert status["claimed"] is False
        assert status["claim_attempts"] == 0
        assert status["copied"] is False
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_wrong_key_pending_counts_auth_failure_not_a_poll(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        async with client_for(provider) as client:
            denied = await client.get("/api/clipboard/pending", headers=WRONG)
            assert denied.status_code == 401
            await pending_item(client)
        status = provider.stage_status(prepared["transaction_id"])
        assert status["pending_auth_failures"] == 1
        assert status["pending_polls"] == 1
        assert status["claimed"] is False
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_claim_without_ack_is_visible_before_any_copy_confirmation(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        async with client_for(provider) as client:
            item = await pending_item(client)
            payload = await client.get(item["payload_url"])
            assert payload.status_code == 200
            assert payload.json() == {"copy_allowed": True, "text": "你好00123"}
            gone = await pending_item(client)
            assert gone == {"pending": False}
        status = provider.stage_status(prepared["transaction_id"])
        assert status["claimed"] is True
        assert status["claimed_at"] is not None
        assert status["claim_attempts"] == 1
        assert status["pending_polls"] == 2
        assert status["ack_attempts"] == 0
        assert status["copied"] is False
        assert status["phase"] == "awaiting_copy"
        # The diagnostics must never carry the pasted text or the token.
        assert "你好00123" not in repr(status)
        assert item["text_url"].rsplit("/", 1)[-1] not in repr(status)
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_legacy_text_claim_also_marks_claimed(monkeypatch):
    """The old '领取待粘贴文本' Shortcut claims via the plain text URL.

    Whichever Shortcut is bound, the claim is visible the same way, so the
    host can still tell "claimed but not acked" apart from "never claimed".
    """
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        async with client_for(provider) as client:
            item = await pending_item(client)
            text = await client.get(item["text_url"])
            assert text.status_code == 200
            assert text.text == "你好00123"
            ack = await client.post(item["ack_url"], json={"copied": True, "focused": True})
            assert ack.status_code == 200
        status = provider.stage_status(prepared["transaction_id"])
        assert status["claimed"] is True
        assert status["claim_attempts"] == 1
        assert status["ack_attempts"] == 1
        assert status["last_ack_error"] is None
        assert status["copied"] is True
        assert status["phase"] == "awaiting_paste"
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_ack_before_claim_is_counted_and_named(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        async with client_for(provider) as client:
            item = await pending_item(client)
            early = await client.post(item["ack_url"], json={"copied": True})
            assert early.status_code == 409
        status = provider.stage_status(prepared["transaction_id"])
        assert status["ack_attempts"] == 1
        assert status["last_ack_error"] == "ack before claim"
        assert status["claimed"] is False
        assert status["copied"] is False
        async with client_for(provider) as client:
            item = await pending_item(client)
            payload = await client.get(item["payload_url"])
            assert payload.status_code == 200
            ack = await client.post(item["ack_url"], json={"copied": True})
            assert ack.status_code == 200
        status = provider.stage_status(prepared["transaction_id"])
        assert status["ack_attempts"] == 2
        assert status["last_ack_error"] is None
        assert status["copied"] is True
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_ack_reporting_no_copy_is_counted_and_named(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        async with client_for(provider) as client:
            item = await pending_item(client)
            payload = await client.get(item["payload_url"])
            assert payload.status_code == 200
            ack = await client.post(item["ack_url"], json={"copied": False})
            assert ack.status_code == 409
        status = provider.stage_status(prepared["transaction_id"])
        assert status["claimed"] is True
        assert status["ack_attempts"] == 1
        assert status["last_ack_error"] == "ack without copied=true"
        assert status["copied"] is False
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())


def test_duplicate_claim_attempt_is_counted(monkeypatch):
    async def scenario():
        screen, provider, phone, control, manager = setup(monkeypatch)
        prepared = await manager.prepare(prepare_body(control))
        async with client_for(provider) as client:
            item = await pending_item(client)
            first = await client.get(item["payload_url"])
            assert first.status_code == 200
            second = await client.get(item["payload_url"])
            assert second.status_code == 404
        status = provider.stage_status(prepared["transaction_id"])
        assert status["claim_attempts"] == 2
        assert status["claimed"] is True
        manager.cancel(prepared["transaction_id"])

    asyncio.run(scenario())
