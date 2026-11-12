import asyncio
import runpy

import httpx
import pytest
from fastapi import FastAPI

from hidagent.clipboard import CompanionClipboard, create_router


def test_clipboard_setup_refuses_public_control_listener(monkeypatch):
    calls = []
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_KEY", "x" * 40)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    with pytest.raises(SystemExit, match="127.0.0.1"):
        runpy.run_module("hidagent", run_name="__main__")
    assert calls == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/frame.jpg"), ("GET", "/api/control/observe"),
    ("POST", "/api/type"), ("POST", "/api/key"), ("POST", "/api/raw"),
    ("GET", "/"), ("GET", "/docs"), ("GET", "/openapi.json"),
    ("POST", "/api/clipboard/pending"), ("DELETE", "/api/clipboard/current"),
    ("GET", "/api/clipboard/../frame.jpg"),
    ("GET", "/api/clipboard/%2e%2e/frame.jpg"),
    ("GET", "/api/clipboard/%252e%252e%252fframe.jpg"),
    ("GET", "/api/clipboard/pending/../../frame.jpg"),
    ("GET", "/api/clipboard/pending?url=http://untrusted.test/api/type"),
])
def test_lan_listener_never_forwards_control_or_ambiguous_routes(method, path):
    from hidagent.clipboard_proxy import create_app

    async def scenario():
        forwarded = []

        def upstream(request):
            forwarded.append(request)
            return httpx.Response(200, text="private control response")

        app = create_app(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lan.test") as client:
            result = await client.request(method, path, json={"text": "must not type"})
        assert result.status_code in (404, 405)
        assert not forwarded

    asyncio.run(scenario())


def test_lan_listener_preserves_pairing_and_full_one_shot_transaction():
    from hidagent.clipboard_proxy import create_app

    async def scenario():
        provider = CompanionClipboard("http://lan.test", "x" * 40)
        control = FastAPI()
        control.include_router(create_router(lambda: provider))
        app = create_app(transport=httpx.ASGITransport(app=control))
        sent = []

        async def send(command):
            sent.append(command)

        task = asyncio.create_task(provider.paste("隔离测试00123", stopped=lambda: False, send=send))
        await asyncio.sleep(0)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lan.test") as client:
                assert (await client.get("/api/clipboard/pending")).status_code == 401
                paired = {"Authorization": "Bearer " + "x" * 40}
                result = await client.get("/api/clipboard/pending", headers=paired)
                assert result.headers["cache-control"] == "no-store"
                item = result.json()
                assert item["text_url"].startswith("http://lan.test/")
                assert (await client.get(item["text_url"])).text == "隔离测试00123"
                assert (await client.get(item["text_url"])).status_code == 404
                assert (await client.post(item["ack_url"], json={"copied": True, "focused": True})).status_code == 200
                for _ in range(100):
                    state = await client.get(item["status_url"])
                    if state.json()["paste_sent"]:
                        break
                    await asyncio.sleep(.01)
                assert sent == ["key cmd+v"]
                assert (await client.get("/api/clipboard/current", headers=paired)).json()["active"]
                assert (await client.post(item["complete_url"], json={"pasted": True})).status_code == 200
                await asyncio.wait_for(task, 1)
                assert (await client.post("/api/clipboard/resolve", headers=paired,
                                          json={"paste_finished": True})).status_code == 200
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())


def test_proxy_upstream_is_fixed_loopback_and_ignores_host_headers():
    from hidagent.clipboard_proxy import create_app

    async def scenario():
        forwarded = []

        def upstream(request):
            forwarded.append(request)
            return httpx.Response(200, json={"pending": False})

        app = create_app(control_port=8897, transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lan.test") as client:
            result = await client.get("/api/clipboard/pending", headers={
                "Host": "untrusted.test", "Authorization": "Bearer test", "X-Forwarded-Host": "untrusted.test"})
        assert result.status_code == 200
        assert str(forwarded[0].url) == "http://127.0.0.1:8897/api/clipboard/pending"
        assert forwarded[0].headers["host"] == "127.0.0.1:8897"
        assert "x-forwarded-host" not in forwarded[0].headers
        assert forwarded[0].headers["authorization"] == "Bearer test"

    asyncio.run(scenario())


def test_proxy_rejects_large_confirmation_before_forwarding():
    from hidagent.clipboard_proxy import create_app

    async def scenario():
        forwarded = []

        def upstream(request):
            forwarded.append(request)
            return httpx.Response(200)

        app = create_app(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lan.test") as client:
            result = await client.post("/api/clipboard/resolve", content=b"x" * 4097)
        assert result.status_code == 413
        assert not forwarded

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["redirect", "connection"])
def test_proxy_upstream_failures_do_not_leak_control_urls(failure):
    from hidagent.clipboard_proxy import create_app

    async def scenario():
        calls = []

        def upstream(request):
            calls.append(request)
            if failure == "connection":
                raise httpx.ConnectError("private failure details", request=request)
            return httpx.Response(307, headers={"Location": "http://127.0.0.1:8890/api/frame.jpg"})

        app = create_app(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lan.test") as client:
            result = await client.get("/api/clipboard/pending")
        assert result.status_code == 502 and len(calls) == 1
        assert "location" not in result.headers
        assert "private failure details" not in result.text

    asyncio.run(scenario())
