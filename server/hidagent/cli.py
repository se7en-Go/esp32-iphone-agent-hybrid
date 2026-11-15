"""Remote controls for the iPhone control server. Run `iphone-agent --help`."""

import argparse
import base64
import json
import sys
from pathlib import Path

import httpx

from .client import IPhoneClient

try:
    from .mcp import serve as serve_mcp
except ImportError:  # mcp 是可选额外依赖；没装时 status / tap 仍然可用
    serve_mcp = None


def _context(command, *, required=False, coordinates=False, target=False):
    command.add_argument("--screen-id", required=required,
                         help="single-use observation ID; selects the guarded MCP action route")
    command.add_argument("--image-output", type=Path,
                         help="save returned evidence image and emit image_path instead of base64")
    if coordinates:
        command.add_argument("--coordinate-space", choices=("normalized", "pixels"), default="normalized")
    if target:
        command.add_argument("--target-box", nargs=4, type=float, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
                             help="observed target bounds, always normalized 0..1")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--url", help="server URL; defaults to IPHONE_AGENT_URL or http://127.0.0.1:8890")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("status", "capabilities", "task-status", "stop", "mcp"):
        sub.add_parser(name)
    for name in ("home", "back"):
        _context(sub.add_parser(name))
    shot = sub.add_parser("screenshot")
    shot.add_argument("output", type=Path)
    observe = sub.add_parser("observe", help="get current image and a single-use screen_id")
    observe.add_argument("output", nargs="?", type=Path, help="optional evidence JPEG output file")
    observe.add_argument("--for-click", action="store_true",
                         help="passively qualify the visible pointer for click-observed; never move it")
    host_click = sub.add_parser("click-observed", help="click once at the pointer visible in a fresh image; never move")
    host_click.add_argument("pointer_x", type=float)
    host_click.add_argument("pointer_y", type=float)
    host_click.add_argument("--target-box", nargs=4, type=float, required=True,
                            metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    host_click.add_argument("--target-label", default="", help="visible target label; required for sending controls")
    _context(host_click, required=True)
    for name, keys in {"point": ("x", "y"), "tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"),
                       "scroll": ("x", "y", "lines"), "long-press": ("x", "y"),
                       "double-tap": ("x", "y"), "circle": ("x", "y", "radius_x", "radius_y")}.items():
        action = sub.add_parser(name, help="coordinates are screen fractions from 0 to 1")
        for key in keys:
            action.add_argument(key, type=int if key == "lines" else float)
        _context(action, required=name in {"point", "long-press", "double-tap", "circle"},
                 coordinates=True, target=name in {"tap", "long-press", "double-tap"})
        if name == "long-press":
            action.add_argument("--duration", type=float, default=.8, help="hold duration, 0.3..3 seconds")
        if name == "circle":
            action.add_argument("--segments", type=int, default=48)
            action.add_argument("--hold", action=argparse.BooleanOptionalAction, default=True,
                                help="pause before releasing to reduce inertia; both settings draw a stroke")
    path = sub.add_parser("path", help="one continuous pointer stroke with 2..256 points")
    path.add_argument("points", help="JSON array of [x,y] pairs, @file.json or - for stdin")
    path.add_argument("--hold", action=argparse.BooleanOptionalAction, default=True,
                      help="pause before releasing to reduce inertia; --no-hold still presses and drags")
    _context(path, required=True, coordinates=True)
    search = sub.add_parser("search-app", help="open Spotlight and enter an ASCII app name; inspect and select result")
    search.add_argument("text")
    _context(search, required=True, coordinates=True)
    for name in ("type", "key"):
        command = sub.add_parser(name)
        command.add_argument("text")
        if name == "type":
            command.add_argument("--mode", choices=("auto", "paste", "keys"), default="auto")
        else:
            command.add_argument("--repeat", type=int, default=1)
            command.add_argument("--enter-intent", choices=("navigation", "editing"))
        _context(command)
    for name in ("find-text", "tap-text"):
        command = sub.add_parser(name)
        command.add_argument("text")
        command.add_argument("--nth", type=int, default=1 if name == "find-text" else None)
        command.add_argument("--exact", action="store_true")
        command.add_argument("--region", nargs=4, type=float, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
        _context(command, required=True)
    compose = sub.add_parser("compose-chinese")
    compose.add_argument("text")
    compose.add_argument("--input-mode", choices=("pinyin",), required=True,
                         help="confirm from the image that the focused keyboard is Pinyin")
    _context(compose, required=True)
    prepare = sub.add_parser("paste-prepare", help="publish text for Shortcut copy; returns without waiting for host")
    prepare.add_argument("text")
    prepare.add_argument("--focused", action="store_true", required=True,
                         help="declare that the current image shows the intended input field focused")
    _context(prepare, required=True, target=True)
    for name in ("paste", "paste-confirm", "paste-status", "paste-cancel"):
        stage = sub.add_parser(name)
        stage.add_argument("transaction_id")
        if name in {"paste", "paste-confirm"}:
            _context(stage, required=True)
        else:
            stage.add_argument("--image-output", type=Path)
        if name == "paste":
            stage.add_argument("--focused", action="store_true", required=True)
        if name == "paste-confirm":
            stage.add_argument("--observed-text", required=True, help="complete exact text verified in returned image")
            stage.add_argument("--confirmed", action="store_true", required=True)
    action = sub.add_parser("action", help="guarded action JSON containing action and screen_id")
    action.add_argument("payload", help="JSON object, @file.json or - for stdin")
    action.add_argument("--image-output", type=Path)
    batch = sub.add_parser("batch", help="guarded batch; inspect evidence before deciding subsequent actions")
    batch.add_argument("steps", help="JSON array, @file.json or - for stdin")
    _context(batch, required=True)
    calibration = sub.add_parser("calibrate")
    calibration.add_argument("--pointer", nargs=2, type=float, metavar=("X", "Y"))
    sub.add_parser("task").add_argument("goal")
    return root


def _json_input(value):
    if value == "-":
        value = sys.stdin.read()
    elif value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    return json.loads(value)


def _save_evidence(result, output):
    if output is not None and result.get("image_base64"):
        result = dict(result)
        output.write_bytes(base64.b64decode(result.pop("image_base64"), validate=True))
        result["image_path"] = str(output.resolve())
    return result


def _action_fields(args):
    names = {
        "point": ("x", "y"), "tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"),
        "scroll": ("x", "y", "lines"), "type": ("text", "mode"), "key": ("text",),
        "home": (), "back": (), "tap-text": ("text", "nth", "exact"),
        "compose-chinese": ("text", "input_mode"),
        "long-press": ("x", "y", "duration"), "double-tap": ("x", "y"),
        "circle": ("x", "y", "radius_x", "radius_y", "segments", "hold"),
        "path": ("hold",), "search-app": ("text",),
    }
    fields = {key: getattr(args, key) for key in names[args.command]}
    if args.command == "path":
        fields["points"] = _json_input(args.points)
    if args.screen_id is not None and not args.screen_id.strip():
        raise ValueError("--screen-id must be nonempty; run observe first")
    if args.screen_id:
        for key in ("coordinate_space", "target_box", "repeat", "enter_intent", "region"):
            value = getattr(args, key, None)
            if value is not None:
                fields[key] = value
    elif (getattr(args, "target_box", None) is not None
          or getattr(args, "coordinate_space", "normalized") != "normalized"
          or getattr(args, "repeat", 1) != 1 or getattr(args, "enter_intent", None) is not None
          or args.image_output is not None):
        raise ValueError("these observation-based options require --screen-id; run observe first")
    return fields


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "mcp":
            if serve_mcp is None:
                print("没有安装 MCP 依赖。请重新运行 setup.sh 或 setup.ps1。", file=sys.stderr)
                return 2
            serve_mcp(args.url)
            return 0
        with IPhoneClient(args.url) as client:
            match args.command:
                case "status": result = client.status()
                case "capabilities": result = client.capabilities()
                case "observe": result = _save_evidence(client.observe(for_click=args.for_click), args.output)
                case "click-observed":
                    result = client.click_observed(args.screen_id, args.pointer_x, args.pointer_y, args.target_box,
                                                   target_label=args.target_label)
                case "screenshot":
                    args.output.write_bytes(client.screenshot())
                    result = {"path": str(args.output.resolve())}
                case "action":
                    payload = _json_input(args.payload)
                    if not isinstance(payload, dict):
                        raise ValueError("action JSON must be an object containing action and screen_id")
                    name, screen_id = payload.pop("action", None), payload.pop("screen_id", None)
                    if not all(isinstance(value, str) and value.strip() for value in (name, screen_id)):
                        raise ValueError("action JSON requires nonempty action and screen_id; run observe first")
                    result = client.control(name, screen_id, **payload)
                case "batch":
                    steps = _json_input(args.steps)
                    if not isinstance(steps, list) or not 1 <= len(steps) <= 8 or not all(
                            isinstance(step, dict) for step in steps):
                        raise ValueError("batch steps must be a JSON array of 1..8 action objects")
                    result = client.batch(args.screen_id, steps)
                case "find-text":
                    result = client.find_text(args.text, args.screen_id, nth=args.nth,
                                              exact=args.exact, region=args.region)
                case "paste-prepare":
                    result = client.clipboard_stage("prepare", text=args.text, screen_id=args.screen_id,
                                                    focused=args.focused,
                                                    **({"target_box": args.target_box} if args.target_box else {}))
                case "paste" | "paste-confirm" | "paste-status" | "paste-cancel":
                    stage = "paste" if args.command == "paste" else args.command.removeprefix("paste-")
                    fields = {key: getattr(args, key) for key in (
                        "transaction_id", "screen_id", "focused", "observed_text", "confirmed") if hasattr(args, key)}
                    result = client.clipboard_stage(stage, **fields)
                case "point" | "tap" | "swipe" | "scroll" | "type" | "key" | "home" | "back" | \
                        "tap-text" | "compose-chinese" | "long-press" | "double-tap" | "circle" | "path" | \
                        "search-app":
                    fields = _action_fields(args)
                    name = "open_app" if args.command == "search-app" else args.command.replace("-", "_")
                    result = (client.control(name, args.screen_id, **fields) if args.screen_id
                              else client.action(name, **fields))
                case "calibrate": result = client.calibrate(*(args.pointer or []))
                case "task": result = client.delegate(args.goal)
                case "task-status": result = client.task_status()
                case "stop": result = client.stop()
        result = _save_evidence(result, getattr(args, "image_output", None))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        text = str(e)
        if "Connection refused" in text or "10061" in text or "积极拒绝" in text:
            text = "连不上本机 8890。请先运行 run-local.sh 或 run-server.ps1，并确认端口没被别的程序占用。"
        print(json.dumps({"error": text}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
