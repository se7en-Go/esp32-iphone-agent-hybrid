import asyncio

import httpx
import pytest
from fastapi import FastAPI

from hidagent.clipboard import CompanionClipboard, create_router
from hidagent.device import DeviceError

KEY = "test_pairing_key_" + "x" * 32
BASE = "http://clipboard.test"
PAIRED = {"Authorization": f"Bearer {KEY}"}
READY = {"copied": True, "focused": True}


def client_for(provider):
    app = FastAPI()
    app.include_router(create_router(lambda: provider))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


async def no_send(command):
    assert command == "key cmd+v"


async def pending(client):
    response = await client.get("/api/clipboard/pending", headers=PAIRED)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def wait_sent(client, item):
    for _ in range(100):
        response = await client.get(item["status_url"])
        assert response.status_code == 200
        if response.json()["paste_sent"]:
            return
        await asyncio.sleep(.01)
    raise AssertionError("paste command was not sent")


async def finish(client, item, task):
    await wait_sent(client, item)
    assert not task.done()
    response = await client.post(item["complete_url"], json={"pasted": True})
    assert response.status_code == 200
    await asyncio.wait_for(task, 1)


def test_clipboard_get_is_one_shot_and_only_completion_unblocks(caplog):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        secret = "中文 13267179555"
        task = asyncio.create_task(provider.paste(secret, stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        async with client_for(provider) as client:
            item = await pending(client)
            assert item["pending"] is True
            assert secret not in str(item)
            assert (await pending(client))["text_url"] == item["text_url"]
            assert (await client.post(item["ack_url"], json=READY)).status_code == 409
            assert (await client.post(item["complete_url"], json={"pasted": True})).status_code == 409
            fetched = await client.get(item["text_url"])
            assert fetched.status_code == 200 and fetched.text == secret
            assert fetched.headers["content-type"] == "text/plain; charset=utf-8"
            assert fetched.headers["cache-control"] == "no-store"
            assert provider._pending.text is None
            again = await client.get(item["text_url"])
            assert again.status_code == 404
            assert again.headers["cache-control"] == "no-store"
            assert not task.done()
            ack = await client.post(item["ack_url"], json=READY)
            assert ack.status_code == 200
            assert ack.headers["cache-control"] == "no-store"
            assert (await client.post(item["ack_url"], json=READY)).status_code == 404
            await finish(client, item, task)
            assert provider._pending is None
            assert (await client.post(item["complete_url"], json={"pasted": True})).status_code == 404
            assert secret not in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", f"Basic {KEY}"])
def test_metadata_and_recovery_require_pairing_header(authorization):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        task = asyncio.create_task(provider.paste("不要泄漏", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        try:
            async with client_for(provider) as client:
                headers = {} if authorization is None else {"Authorization": authorization}
                for method, route in (("GET", "pending"), ("GET", "current"), ("POST", "resolve")):
                    response = await client.request(method, f"/api/clipboard/{route}", headers=headers, json={})
                    assert response.status_code == 401
                    assert response.headers["cache-control"] == "no-store"
                    assert "不要泄漏" not in response.text
                    assert provider.pending() is not None
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_unknown_tokens_and_disabled_routes_are_closed():
    async def scenario():
        for provider in (None, CompanionClipboard(BASE, KEY)):
            async with client_for(provider) as client:
                for method, url in (("GET", "/api/clipboard/pending"),
                                    ("GET", "/api/clipboard/unknown"),
                                    ("GET", "/api/clipboard/中文"),
                                    ("GET", "/api/clipboard/unknown/status"),
                                    ("POST", "/api/clipboard/unknown/ack"),
                                    ("POST", "/api/clipboard/unknown/complete")):
                    response = await client.request(method, url, headers=PAIRED, json={})
                    if provider is not None and url.endswith("pending"):
                        assert response.status_code == 200 and response.json() == {"pending": False}
                    else:
                        assert response.status_code == 404
                    assert response.headers["cache-control"] == "no-store"

    asyncio.run(scenario())


@pytest.mark.parametrize("after_fetch", [False, True])
def test_expiration_purges_text_without_a_followup_request(after_fetch):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY, ttl=1)
        task = asyncio.create_task(provider.paste("过期内容", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        original = provider._pending
        item = provider.pending()
        if after_fetch:
            async with client_for(provider) as client:
                assert (await client.get(item["text_url"])).status_code == 200
        with pytest.raises(DeviceError, match="超时"):
            await task
        assert original.expired and original.text is None
        assert provider._pending is None
        async with client_for(provider) as client:
            assert (await client.get(item["text_url"])).status_code == 404
            assert (await client.post(item["ack_url"], json=READY)).status_code == 404

    asyncio.run(scenario())


def test_already_stopped_does_not_publish_text():
    provider = CompanionClipboard(BASE, KEY)
    with pytest.raises(DeviceError, match="紧急停止"):
        asyncio.run(provider.paste("never published", stopped=lambda: True, send=no_send))
    assert provider._pending is None


def test_cancel_clears_pending_and_old_token_cannot_ack_new_request():
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        first = asyncio.create_task(provider.paste("old", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        original = provider._pending
        old = provider.pending()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert original.text is None and provider.pending() is None
        second = asyncio.create_task(provider.paste("new", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        try:
            async with client_for(provider) as client:
                current = await pending(client)
                assert current["text_url"] != old["text_url"]
                assert (await client.post(old["ack_url"], json=READY)).status_code == 404
                assert (await client.get(current["text_url"])).text == "new"
                assert (await client.post(current["ack_url"], json=READY)).status_code == 200
                await finish(client, current, second)
        finally:
            if not second.done():
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second

    asyncio.run(scenario())


@pytest.mark.parametrize("after_fetch", [False, True])
def test_stop_clears_pending_without_confirming(after_fetch):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        stop = False
        task = asyncio.create_task(provider.paste("stop me", stopped=lambda: stop, send=no_send))
        await asyncio.sleep(0)
        item = provider.pending()
        async with client_for(provider) as client:
            if after_fetch:
                assert (await client.get(item["text_url"])).status_code == 200
            stop = True
            with pytest.raises(DeviceError, match="紧急停止"):
                await asyncio.wait_for(task, 1)
            assert provider._pending is None
            assert (await client.post(item["ack_url"], json=READY)).status_code == 404

    asyncio.run(scenario())


def test_busy_rejects_new_text_without_replacing_pending():
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        first = asyncio.create_task(provider.paste("first", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        item = provider.pending()
        with pytest.raises(DeviceError, match="上一次"):
            await provider.paste("second", stopped=lambda: False, send=no_send)
        async with client_for(provider) as client:
            assert (await client.get(item["text_url"])).text == "first"
            await client.post(item["ack_url"], json=READY)
            await wait_sent(client, item)
            with pytest.raises(DeviceError, match="上一次"):
                await provider.paste("second", stopped=lambda: False, send=no_send)
            assert (await client.post("/api/clipboard/resolve", headers=PAIRED,
                                      json={"paste_finished": True})).status_code == 409
            await finish(client, item, first)

    asyncio.run(scenario())


def test_concurrent_gets_have_one_winner():
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        task = asyncio.create_task(provider.paste("one", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        async with client_for(provider) as client:
            item = await pending(client)
            responses = await asyncio.gather(client.get(item["text_url"]), client.get(item["text_url"]))
            assert sorted(response.status_code for response in responses) == [200, 404]
            await client.post(item["ack_url"], json=READY)
            await finish(client, item, task)

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["", "x" * 4001])
def test_invalid_lengths_never_create_pending(text):
    provider = CompanionClipboard(BASE, KEY)
    with pytest.raises(DeviceError, match="1..4000"):
        asyncio.run(provider.paste(text, stopped=lambda: False, send=no_send))
    assert provider._pending is None


@pytest.mark.parametrize("url,key,ttl", [
    ("file:///tmp/text", KEY, 60),
    ("http://user:pass@clipboard.test", KEY, 60),
    ("http://clipboard.test/?secret=x", KEY, 60),
    ("http://clipboard.test/#fragment", KEY, 60),
    ("http://clipboard.test:invalid", KEY, 60),
    (BASE, "short", 60),
    (BASE, "x" * 31 + "汉", 60),
    (BASE, KEY, 0),
    (BASE, KEY, 301),
    (BASE, KEY, float("nan")),
])
def test_configuration_validation(url, key, ttl):
    with pytest.raises(ValueError, match="HIDAGENT_CLIPBOARD_"):
        CompanionClipboard(url, key, ttl)


@pytest.mark.parametrize("trigger", ["cmd+v", "ctrl+ctrl+p", "enter", "ctrl+p\nkey cmd+v", "alt+中文"])
def test_trigger_key_configuration_validation(trigger):
    with pytest.raises(ValueError, match="HIDAGENT_CLIPBOARD_TRIGGER_KEY"):
        CompanionClipboard(BASE, KEY, trigger_key=trigger)


def test_configuration_from_environment(monkeypatch):
    for name in ("BASE_URL", "KEY", "TTL", "TRIGGER_KEY", "PROTOCOL"):
        monkeypatch.delenv("HIDAGENT_CLIPBOARD_" + name, raising=False)
    assert CompanionClipboard.from_env() is None
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_KEY", KEY)
    with pytest.raises(ValueError, match="同时设置"):
        CompanionClipboard.from_env()
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_BASE_URL", BASE + "/")
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_TRIGGER_KEY", "ctrl+alt+p")
    provider = CompanionClipboard.from_env()
    assert provider.base_url == BASE and provider.ttl == 60 and provider.trigger_key == "ctrl+alt+p"
    monkeypatch.setenv("HIDAGENT_CLIPBOARD_TTL", "invalid")
    with pytest.raises(ValueError, match="HIDAGENT_CLIPBOARD_TTL"):
        CompanionClipboard.from_env()


@pytest.mark.parametrize("body,status", [({}, 409), ({"copied": True}, 409),
    ({"copied": True, "focused": False}, 409), ({"copied": "true", "focused": True}, 422),
    ({"copied": True, "focused": 1}, 422)])
def test_copy_and_focus_must_both_be_explicitly_confirmed(body, status):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        task = asyncio.create_task(provider.paste("copy", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        try:
            async with client_for(provider) as client:
                item = await pending(client)
                await client.get(item["text_url"])
                assert (await client.post(item["ack_url"], json=body)).status_code == status
                assert not provider._pending.acknowledged
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_uncertain_paste_blocks_new_copy_until_paired_resolution(cancel):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY, ttl=1)
        task = asyncio.create_task(provider.paste("first", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        async with client_for(provider) as client:
            item = await pending(client)
            await client.get(item["text_url"])
            await client.post(item["ack_url"], json=READY)
            await wait_sent(client, item)
            current = await client.get("/api/clipboard/current", headers=PAIRED)
            assert current.json()["complete_url"] == item["complete_url"]
            assert current.json()["paste_sent"] is True
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else DeviceError):
                await task
            assert (await client.get(item["text_url"])).status_code == 404
            assert (await client.post(item["complete_url"], json={"pasted": True})).status_code == 404
            assert (await client.get("/api/clipboard/current", headers=PAIRED)).json() == {
                "active": False, "needs_resolution": True}
            with pytest.raises(DeviceError, match="阻止"):
                await provider.paste("second", stopped=lambda: False, send=no_send)
            assert provider.pending() is None
            assert (await client.post("/api/clipboard/resolve", headers=PAIRED, json={})).status_code == 409
            assert (await client.post("/api/clipboard/resolve", headers=PAIRED,
                                      json={"paste_finished": True})).status_code == 200
            next_task = asyncio.create_task(provider.paste("second", stopped=lambda: False, send=no_send))
            await asyncio.sleep(0)
            assert provider.pending() is not None
            next_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await next_task

    asyncio.run(scenario())


@pytest.mark.parametrize("hang_at", ["trigger", "paste"])
def test_deadline_cancels_hanging_device_send(hang_at):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY, ttl=1, trigger_key="ctrl+alt+p")
        cancelled = asyncio.Event()

        async def send(command):
            if command == ("key ctrl+alt+p" if hang_at == "trigger" else "key cmd+v"):
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        task = asyncio.create_task(provider.paste("first", stopped=lambda: False, send=send))
        await asyncio.sleep(0)
        if hang_at == "paste":
            async with client_for(provider) as client:
                item = await pending(client)
                await client.get(item["text_url"])
                await client.post(item["ack_url"], json=READY)
        with pytest.raises(DeviceError, match="超时"):
            await asyncio.wait_for(task, 2)
        assert cancelled.is_set() and provider._pending is None
        assert provider.current()["needs_resolution"] is (hang_at == "paste")

    asyncio.run(scenario())


def test_fetched_then_cancelled_consumer_must_finish_before_new_publication():
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        first = asyncio.create_task(provider.paste("old", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        async with client_for(provider) as client:
            item = await pending(client)
            assert (await client.get(item["text_url"])).text == "old"
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(DeviceError, match="阻止"):
                await provider.paste("new", stopped=lambda: False, send=no_send)
            assert provider.pending() is None
            assert (await client.post(item["ack_url"], json=READY)).status_code == 404
            assert (await client.post("/api/clipboard/resolve", headers=PAIRED,
                                      json={"paste_finished": True})).status_code == 200
            next_task = asyncio.create_task(provider.paste("new", stopped=lambda: False, send=no_send))
            await asyncio.sleep(0)
            assert provider.pending() is not None
            next_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await next_task

    asyncio.run(scenario())


@pytest.mark.parametrize("body,status", [({}, 409), ({"pasted": False}, 409),
                                        ({"pasted": "true"}, 422), ({"pasted": 1}, 422)])
def test_completion_requires_true_boolean_after_paste(body, status):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        task = asyncio.create_task(provider.paste("one", stopped=lambda: False, send=no_send))
        await asyncio.sleep(0)
        async with client_for(provider) as client:
            item = await pending(client)
            await client.get(item["text_url"])
            await client.post(item["ack_url"], json=READY)
            await wait_sent(client, item)
            assert (await client.post(item["complete_url"], json=body)).status_code == status
            assert not task.done() and not provider._pending.completed
            await finish(client, item, task)

    asyncio.run(scenario())
