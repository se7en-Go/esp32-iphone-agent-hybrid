import asyncio

import httpx
from fastapi import FastAPI

from hidagent.clipboard import CompanionClipboard, create_router
from hidagent.phone import Phone

KEY = "lifecycle_pairing_key_" + "x" * 32
BASE = "http://clipboard.test"


def test_next_copy_waits_for_the_previous_text_to_be_consumed(tmp_path):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY)
        app = FastAPI()
        app.include_router(create_router(lambda: provider))
        clipboard = ""
        consumed = []
        completion_urls = []
        readers = []

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
            class Device:
                async def send(self, command):
                    assert command == "key cmd+v"
                    completion_url = completion_urls[-1]

                    async def consume_later():
                        await asyncio.sleep(.05)
                        consumed.append(clipboard)
                        response = await client.post(completion_url, json={"pasted": True})
                        assert response.status_code == 200

                    readers.append(asyncio.create_task(consume_later()))

            phone = Phone(Device(), tmp_path / "cal.json", clipboard=provider)

            async def consumer():
                nonlocal clipboard
                for _ in range(2):
                    while True:
                        response = await client.get("/api/clipboard/pending",
                                                    headers={"Authorization": f"Bearer {KEY}"})
                        item = response.json()
                        if item.get("pending"):
                            break
                        await asyncio.sleep(.001)
                    clipboard = (await client.get(item["text_url"])).text
                    completion_urls.append(item["complete_url"])
                    response = await client.post(item["ack_url"], json={"copied": True, "focused": True})
                    assert response.status_code == 200

            consuming = asyncio.create_task(consumer())
            await asyncio.wait_for(asyncio.gather(phone.type("first"), phone.type("second"), consuming), 3)
            await asyncio.gather(*readers)
            assert consumed == ["first", "second"]

    asyncio.run(scenario())


def test_configured_server_trigger_runs_consumer_before_one_confirmed_paste(tmp_path):
    async def scenario():
        provider = CompanionClipboard(BASE, KEY, trigger_key="ctrl+alt+p")
        app = FastAPI()
        app.include_router(create_router(lambda: provider))
        copied, focused, paste_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        commands, consumed, consumers = [], [], []
        clipboard = ""
        expected = "你好 0013267179555"

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
            async def shortcut():
                nonlocal clipboard
                response = await client.get("/api/clipboard/pending",
                                            headers={"Authorization": f"Bearer {KEY}"})
                item = response.json()
                assert item["pending"] is True
                clipboard = (await client.get(item["text_url"])).text
                copied.set()
                await focused.wait()
                response = await client.post(item["ack_url"], json={"copied": True, "focused": True})
                assert response.status_code == 200
                await paste_sent.wait()
                await asyncio.sleep(.05)
                consumed.append(clipboard)
                response = await client.post(item["complete_url"], json={"pasted": True})
                assert response.status_code == 200

            class Device:
                async def send(self, command):
                    commands.append(command)
                    if command == "key ctrl+alt+p":
                        assert provider.pending() is not None
                        consumers.append(asyncio.create_task(shortcut()))
                    elif command == "key cmd+v":
                        assert copied.is_set() and focused.is_set()
                        paste_sent.set()
                    else:
                        raise AssertionError(command)

            phone = Phone(Device(), tmp_path / "cal.json", clipboard=provider)
            typing = asyncio.create_task(phone.type(expected, mode="paste"))
            await asyncio.wait_for(copied.wait(), 1)
            assert commands == ["key ctrl+alt+p"] and not typing.done()
            focused.set()
            await asyncio.wait_for(typing, 2)
            await asyncio.gather(*consumers)
            assert commands == ["key ctrl+alt+p", "key cmd+v"]
            assert consumed == [expected]
            assert provider.current() == {"active": False, "needs_resolution": False}

    asyncio.run(scenario())
