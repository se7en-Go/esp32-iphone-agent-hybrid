import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from hidagent import mcp as module  # noqa: E402


@pytest.mark.parametrize("region", [None, [.1, .2, .9, .8]])
def test_ocr_tools_forward_queries_and_preserve_image_and_diff(monkeypatch, region):
    calls = []
    scope = {"region": region} if region is not None else {}

    class Client:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, path, body):
            calls.append((method, path, body))
            return SimpleNamespace(json=lambda: {"screen_id": body["screen_id"],
                                                 "match": {"text": "通用", "center": [.2, .35]}})

        def control(self, name, screen_id, **args):
            calls.append((name, screen_id, args))
            return {"screen_id": "after", "command_acknowledged": True, "effect_verified": False,
                    "screen_diff": {"changed_fraction": .4},
                    "image_base64": base64.b64encode(b"after image").decode()}

    monkeypatch.setattr(module, "IPhoneClient", Client)
    server = module.create_server()

    async def scenario():
        tools = {tool.name: tool for tool in await server.list_tools()}
        for name in ("iphone_find_text", "iphone_tap_text"):
            assert set(tools[name].inputSchema["required"]) == {"screen_id", "text"}
        found = await server.call_tool("iphone_find_text", {
            "screen_id": "observed", "text": "通用", "nth": 2, "exact": True, **scope})
        tapped = await server.call_tool("iphone_tap_text", {"screen_id": "observed", "text": "通用", **scope})
        # FastMCP versions may return (content, structured_content).
        found = found[0] if isinstance(found, tuple) else found
        tapped = tapped[0] if isinstance(tapped, tuple) else tapped
        assert json.loads(found[0].text)["screen_id"] == "observed"
        evidence = json.loads(tapped[0].text)
        assert evidence["screen_id"] == "after" and evidence["effect_verified"] is False
        assert evidence["screen_diff"] == {"changed_fraction": .4}
        assert tapped[1].type == "image" and base64.b64decode(tapped[1].data) == b"after image"

    asyncio.run(scenario())
    assert calls == [("POST", "/api/control/find_text", {
        "screen_id": "observed", "text": "通用", "nth": 2, "exact": True, **scope}),
        ("tap_text", "observed", {"text": "通用", "nth": None, "exact": False, **scope})]
