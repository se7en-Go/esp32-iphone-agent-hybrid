import asyncio
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def test_real_stdio_uses_host_vision_and_returns_action_images_without_model_keys(monkeypatch):
    requests = []
    observations = []
    clipboard_steps = [
        ("prepare", {"text": "00123\t中文\r\n第二行", "screen_id": "input-frame", "focused": True,
                     "target_box": [.1, .2, .8, .4]}),
        ("status", {"transaction_id": "transaction-1"}),
        ("paste", {"transaction_id": "transaction-1", "screen_id": "after-copy", "focused": True}),
        ("confirm", {"transaction_id": "transaction-1", "screen_id": "after-paste",
                     "observed_text": "00123\t中文\r\n第二行", "confirmed": True}),
        ("cancel", {"transaction_id": "transaction-2"}),
    ]
    monkeypatch.delenv("IPHONE_MCP_ENABLE_DELEGATE", raising=False)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/api/control/observe":
                observations.append(self.path)
                self.reply({"screen_id": "one-use", "width": 608, "height": 1080,
                            "image_base64": base64.b64encode(b"synthetic jpeg").decode()})
            elif self.path == "/api/control/capabilities":
                self.reply({"actions": ["tap", "path", "circle", "long_press", "double_tap", "open_app"],
                            "features": {"multitouch": {"supported": False}}})
            else:
                self.reply({"agent": {"running": False, "task": "", "steps": []}, "models": {}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
            requests.append((self.path, body))
            if self.path == "/api/skill/check":
                self.reply({"stop": "操作频繁" in (body or {}).get("observed_text", ""), "allowed": True})
                return
            self.reply({"command_acknowledged": True, "effect_verified": False,
                        "image_base64": base64.b64encode(b"after action").decode()})

        def log_message(self, *_): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "ACTOR_", "PLANNER_"))}
        clean["IPHONE_MCP_ENABLE_DELEGATE"] = "0"
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "hidagent.cli", "--url", f"http://127.0.0.1:{server.server_port}", "mcp"], env=clean)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {"iphone_observe", "iphone_tap", "iphone_compose_chinese",
                    "task_status", "stop_task"} <= tools.keys()
            gestures = {"iphone_path", "iphone_circle", "iphone_long_press", "iphone_double_tap",
                        "iphone_search_app"}
            assert gestures <= tools.keys()
            for name in gestures:
                assert "screen_id" in tools[name].inputSchema["required"]
            assert "still presses and drags" in tools["iphone_path"].description
            assert "either value draws" in tools["iphone_circle"].description
            assert "never assume those keys selected" in tools["iphone_compose_chinese"].description
            assert "space) selects it" not in tools["iphone_compose_chinese"].description
            capabilities = await session.call_tool("iphone_capabilities", {})
            assert not capabilities.isError
            assert json.loads(capabilities.content[0].text)["features"]["multitouch"]["supported"] is False
            assert "delegate_task" not in tools
            assert "guard_reset" not in tools
            assert {"iphone_batch", "iphone_layouts", "skill_check", "skill_confirm_send"} <= tools.keys()
            assert "screen_id" in tools["iphone_tap"].inputSchema["required"]
            assert "bubble" in tools["skill_confirm_send"].description
            shot = await session.call_tool("iphone_screenshot", {})
            assert not shot.isError
            assert json.loads(shot.content[0].text)["screen_id"] == "one-use"
            assert shot.content[1].type == "image" and shot.content[1].mimeType == "image/jpeg"
            qualified_shot = await session.call_tool("iphone_observe", {"for_click": True})
            assert not qualified_shot.isError and qualified_shot.content[1].type == "image"
            assert observations == ["/api/control/observe", "/api/control/observe?for_click=true"]
            missing_token = await session.call_tool("iphone_tap", {"x": .5, "y": .4})
            assert missing_token.isError and not requests
            result = await session.call_tool("iphone_tap", {"x": .5, "y": .4, "screen_id": "one-use"})
            assert not result.isError and result.content[1].type == "image"
            assert json.loads(result.content[0].text)["effect_verified"] is False
            edited = await session.call_tool("iphone_key", {
                "combo": "backspace", "screen_id": "after-observation", "repeat": 3})
            assert not edited.isError and edited.content[1].type == "image"
            checked = await session.call_tool(
                "skill_check", {"observed_text": "操作频繁", "skill": "wechat-group-reply"})
            assert not checked.isError and json.loads(checked.content[0].text)["stop"] is True
            batched = await session.call_tool("iphone_batch", {"screen_id": "batch-1", "steps": [
                {"action": "type", "text": "hi"}]})
            assert not batched.isError and batched.content[1].type == "image"
            assert not (await session.call_tool("stop_task", {})).isError
            guidance = await session.call_tool("iphone_model_help", {})
            assert not guidance.isError and "No model calls" in tools["iphone_model_help"].description
            for name, arguments in [
                ("iphone_path", {"points": [[.1, .2], [.2, .3]], "hold": False}),
                ("iphone_circle", {"x": .5, "y": .4, "radius_x": .2, "radius_y": .1}),
                ("iphone_long_press", {"x": .5, "y": .4, "duration": 1.2,
                                       "target_box": [.4, .3, .6, .5]}),
                ("iphone_double_tap", {"x": .5, "y": .4}),
                ("iphone_search_app", {"text": "Freeform"}),
            ]:
                result = await session.call_tool(name, {**arguments, "screen_id": "seen-gesture"})
                assert not result.isError and result.content[1].type == "image"
                assert json.loads(result.content[0].text)["effect_verified"] is False
            for stage, arguments in clipboard_steps:
                name = "iphone_paste" if stage == "paste" else "iphone_paste_" + stage
                result = await session.call_tool(name, arguments)
                assert not result.isError
                assert json.loads(result.content[0].text)["effect_verified"] is False
                if stage != "cancel":
                    assert result.content[1].type == "image"
            before_invalid = len(requests)
            refused = await session.call_tool("iphone_paste", {
                "transaction_id": "transaction-1", "screen_id": "fresh", "focused": False})
            assert refused.isError and len(requests) == before_invalid
            host_click = tools["iphone_click_observed"]
            assert {"pointer_x", "pointer_y", "screen_id", "target_box"} <= set(
                host_click.inputSchema["required"])
            assert "Do not infer the pointer" in host_click.description
            missing_pointer = await session.call_tool("iphone_click_observed", {
                "screen_id": "fresh", "target_box": [.4, .3, .6, .5]})
            assert missing_pointer.isError and len(requests) == before_invalid
            observed_click = await session.call_tool("iphone_click_observed", {
                "pointer_x": .5, "pointer_y": .4, "screen_id": "host-saw-pointer",
                "target_box": [.4, .3, .6, .5], "target_label": "主屏幕"})
            assert not observed_click.isError and observed_click.content[1].type == "image"
            assert json.loads(observed_click.content[0].text)["effect_verified"] is False
            observed_hold = await session.call_tool("iphone_click_observed", {
                "pointer_x": .5, "pointer_y": .4, "screen_id": "host-saw-hold",
                "target_box": [.4, .3, .6, .5], "target_label": "AssistiveTouch", "duration": .8})
            assert not observed_hold.isError
            before_invalid_hold = len(requests)
            invalid_hold = await session.call_tool("iphone_click_observed", {
                "pointer_x": .5, "pointer_y": .4, "screen_id": "invalid-hold",
                "target_box": [.4, .3, .6, .5], "duration": .2})
            assert invalid_hold.isError and len(requests) == before_invalid_hold

    try:
        asyncio.run(asyncio.wait_for(scenario(), 25))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert requests == [("/api/control/action", {"action": "tap", "screen_id": "one-use", "x": .5, "y": .4,
                                               "coordinate_space": "normalized"}),
                        ("/api/control/action", {"action": "key", "screen_id": "after-observation",
                                                "text": "backspace", "repeat": 3}),
                        ("/api/skill/check", {"observed_text": "操作频繁", "skill": "wechat-group-reply"}),
                        ("/api/control/batch", {"screen_id": "batch-1", "steps": [
                            {"action": "type", "text": "hi"}]}),
                        ("/api/agent/stop", {}),
                        ("/api/control/action", {"action": "path", "screen_id": "seen-gesture",
                         "points": [[.1, .2], [.2, .3]], "hold": False, "coordinate_space": "normalized"}),
                        ("/api/control/action", {"action": "circle", "screen_id": "seen-gesture",
                         "x": .5, "y": .4, "radius_x": .2, "radius_y": .1, "segments": 48,
                         "hold": True, "coordinate_space": "normalized"}),
                        ("/api/control/action", {"action": "long_press", "screen_id": "seen-gesture",
                         "x": .5, "y": .4, "duration": 1.2, "target_box": [.4, .3, .6, .5],
                         "coordinate_space": "normalized"}),
                        ("/api/control/action", {"action": "double_tap", "screen_id": "seen-gesture",
                         "x": .5, "y": .4, "coordinate_space": "normalized"}),
                        ("/api/control/action", {"action": "open_app", "screen_id": "seen-gesture",
                         "text": "Freeform", "coordinate_space": "normalized"})] + [
                        ("/api/control/clipboard/" + stage, body) for stage, body in clipboard_steps] + [
                        ("/api/control/host-click", {"screen_id": "host-saw-pointer", "pointer_x": .5,
                         "pointer_y": .4, "target_box": [.4, .3, .6, .5], "target_label": "主屏幕"}),
                        ("/api/control/host-click", {"screen_id": "host-saw-hold", "pointer_x": .5,
                         "pointer_y": .4, "target_box": [.4, .3, .6, .5], "target_label": "AssistiveTouch",
                         "duration": .8})]


def test_delegate_tool_requires_explicit_opt_in(monkeypatch):
    from hidagent.mcp import create_server
    monkeypatch.setenv("IPHONE_MCP_ENABLE_DELEGATE", "1")
    tools = asyncio.run(create_server().list_tools())
    assert "delegate_task" in {tool.name for tool in tools}


def test_mcp_has_no_guard_reset():
    from hidagent.mcp import create_server
    tools = asyncio.run(create_server().list_tools())
    assert "guard_reset" not in {tool.name for tool in tools}


def test_staged_clipboard_tools_require_host_observation_and_do_not_auto_confirm():
    from hidagent.mcp import create_server
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    assert {"iphone_paste_prepare", "iphone_paste", "iphone_paste_confirm", "iphone_paste_status",
            "iphone_paste_cancel"} <= tools.keys()
    assert {"screen_id", "focused", "text"} <= set(tools["iphone_paste_prepare"].inputSchema["required"])
    assert {"screen_id", "transaction_id", "observed_text", "confirmed"} <= set(
        tools["iphone_paste_confirm"].inputSchema["required"])
    assert "not" in tools["iphone_paste_status"].description.lower()
