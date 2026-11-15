import asyncio

import httpx
from fastapi import FastAPI

from hidagent.clipboard import CompanionClipboard, create_router
from hidagent.clipboard_proxy import create_app


def test_staged_lan_payload_has_no_host_nonce_and_errors_never_authorize_copy():
    async def scenario():
        provider = CompanionClipboard("http://lan.test", "x" * 40)
        control = FastAPI()
        control.include_router(create_router(lambda: provider))
        proxy = create_app(transport=httpx.ASGITransport(app=control))
        nonce = provider.stage_begin("中文00123")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://lan.test") as client:
            assert (await client.get("/api/clipboard/pending")).status_code == 401
            pending = await client.get("/api/clipboard/pending", headers={"Authorization": "Bearer " + "x" * 40})
            assert nonce not in pending.text
            item = pending.json()
            assert item["protocol"] == "staged-v1"
            assert (await client.post(item["ack_url"], json={"copied": True})).status_code == 409
            payload = await client.get(item["payload_url"])
            assert payload.status_code == 200
            assert payload.headers["cache-control"] == "no-store"
            assert payload.json() == {"copy_allowed": True, "text": "中文00123"}
            again = await client.get(item["payload_url"])
            assert again.status_code == 404 and "copy_allowed" not in again.json()
            assert (await client.post(item["ack_url"], json={"copied": True})).status_code == 200
            assert (await client.post(item["complete_url"], json={"pasted": True})).status_code == 409
            assert (await client.post("/api/control/clipboard/paste", json={
                "transaction_id": nonce, "screen_id": "anything", "focused": True})).status_code == 404
            assert (await client.get(item["payload_url"] + "?anything=true")).status_code == 404
        provider.stage_finish(nonce)

    asyncio.run(scenario())


def test_expired_unfetched_payload_cannot_copy_an_error_body():
    async def scenario():
        provider = CompanionClipboard("http://lan.test", "x" * 40)
        control = FastAPI()
        control.include_router(create_router(lambda: provider))
        proxy = create_app(transport=httpx.ASGITransport(app=control))
        provider.stage_begin("private draft")
        url = provider.pending()["payload_url"]
        provider._expire(provider._pending)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://lan.test") as client:
            response = await client.get(url)
            assert response.status_code == 404
            assert "copy_allowed" not in response.json() and "private draft" not in response.text

    asyncio.run(scenario())
