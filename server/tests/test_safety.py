"""熔断挡住 HID，以及网页接口不接受跨站表单。"""

import asyncio

import httpx

from hidagent import app as module


def test_tripped_guard_blocks_hid_and_cross_site_posts():
    module.guard.tripped = True
    module.guard.reason = "操作频繁"
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                blocked = await client.post("/api/key", json={"text": "enter"})
                assert blocked.status_code == 409 and "熔断" in blocked.json()["detail"]
                stopped = await client.post("/api/agent/stop", json={})
                assert stopped.status_code == 200
                evil = await client.post(
                    "/api/key", json={"text": "a"}, headers={"origin": "https://evil.example"})
                assert evil.status_code == 403
                form = await client.post(
                    "/api/key", content=b"text=a",
                    headers={"content-type": "application/x-www-form-urlencoded"})
                assert form.status_code == 415

        asyncio.run(scenario())
    finally:
        module.guard.reset()
