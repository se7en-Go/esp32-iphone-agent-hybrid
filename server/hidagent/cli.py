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
    for name in ("status", "capabilities", "task-status", "stop", "mcp", "layouts", "skill-list"):
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
    host_click.add_argument("--duration", type=float, help="hold the stationary pointer for 0.3..3 seconds")
    _context(host_click, required=True)
    for name, keys in {"point": ("x", "y"), "tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"),
                       "scroll": ("x", "y", "lines"), "long-press": ("x", "y"),
                       "double-tap": ("x", "y"), "circle": ("x", "y", "radius_x", "radius_y")}.items():
        action = sub.add_parser(name, help="coordinates are screen fractions from 0 to 1")
        for key in keys:
            action.add_argument(key, type=int if key == "lines" else float)
        _context(action, required=name in {"point", "long-press", "double-tap", "circle"},
                 coordinates=True, target=name in {"point", "tap", "long-press", "double-tap"})
        if name == "point":
            action.add_argument("--rehome", action="store_true", help="re-home pointer to origin before moving")
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
        if name == "paste-status":
            stage.add_argument("--include-observation", action=argparse.BooleanOptionalAction, default=True,
                               help="include image_base64 and a new screen_id (disable while polling copied)")
        if name == "paste":
            stage.add_argument("--focused", action="store_true", required=True)
        if name == "paste-confirm":
            stage.add_argument("--observed-text", required=True, help="complete exact text verified in returned image")
            stage.add_argument("--confirmed", action="store_true", required=True)
    flow = sub.add_parser("paste-flow", help="timed staged paste via AssistiveTouch Shortcut; stops before Send")
    flow.add_argument("text", help="exact text, @file.txt or - for stdin")
    flow.add_argument("--target-box", nargs=4, type=float, required=True,
                      metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"), help="observed empty input field")
    flow.add_argument("--expect-title", help="exact conversation/page title that must be visible")
    flow.add_argument("--caret-box", nargs=4, type=float, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
                      help="schedule host clicks after a green caret blink edge in this box")
    flow.add_argument("--prewarm", action="store_true", help="hold AssistiveTouch once before prepare")
    flow.add_argument("--full-status", action="store_true", help="poll status with the image (old servers)")
    flow.add_argument("--image-output", type=Path, help="save the confirmation evidence image")
    flow.add_argument("--evidence-dir", type=Path, help="save each stage's observed frame here (local only)")
    action = sub.add_parser("action", help="guarded action JSON containing action and screen_id")
    action.add_argument("payload", help="JSON object, @file.json or - for stdin")
    action.add_argument("--image-output", type=Path)
    batch = sub.add_parser("batch", help="guarded batch; inspect evidence before deciding subsequent actions")
    batch.add_argument("steps", help="JSON array, @file.json or - for stdin")
    _context(batch, required=True)
    calibration = sub.add_parser("calibrate")
    calibration.add_argument("--pointer", nargs=2, type=float, metavar=("X", "Y"))
    middle = sub.add_parser(
        "middle-click-home",
        help="inspect, verify+enable, or revoke BLE mouse middle-button Home routing",
    )
    middle_sub = middle.add_subparsers(dest="middle_action", required=True)
    middle_sub.add_parser("status", help="show firmware support and per-device verification status")
    middle_enable = middle_sub.add_parser(
        "enable",
        help="verify `middleclick` -> Home from an observed non-Home screen and persist for this device",
    )
    _context(middle_enable, required=True)
    middle_revoke = middle_sub.add_parser("revoke", help="disable and forget middle-click Home routing")
    middle_revoke.add_argument("--all", action="store_true", help="revoke verification for all stored devices")
    layout_tap = sub.add_parser("layout-tap", help="tap a named point from skills/*/layout.json")
    layout_tap.add_argument("layout")
    layout_tap.add_argument("point")
    _context(layout_tap, required=True)
    sub.add_parser("skill-show", help="read a skill SKILL.md by name").add_argument("name")
    skill_check = sub.add_parser("skill-check", help="circuit-breaker check for on-screen risk phrases")
    skill_check.add_argument("--observed-text", default="")
    skill_check.add_argument("--skill", default="")
    skill_prepare = sub.add_parser("skill-prepare-send", help="check quiet hours and reserve a send slot")
    skill_prepare.add_argument("--skill", default="")
    skill_confirm = sub.add_parser("skill-confirm-send", help="confirm send only after seeing the new bubble")
    skill_confirm.add_argument("--bubble-visible", action=argparse.BooleanOptionalAction, required=True,
                               help="confirm visible new message bubble on the latest screen")
    skill_confirm.add_argument("--observed-text", default="")
    skill_confirm.add_argument("--skill", default="")
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
        if args.command == "point" and getattr(args, "rehome", False):
            fields["rehome"] = True
    elif (getattr(args, "target_box", None) is not None
          or getattr(args, "rehome", False)
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
                                                   target_label=args.target_label,
                                                   **({"duration": args.duration} if args.duration is not None else {}))
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
                case "paste-flow":
                    from .paste_flow import FlowStopped, run
                    text = args.text
                    if text == "-":
                        text = sys.stdin.read()
                    elif text.startswith("@"):
                        text = Path(text[1:]).read_text(encoding="utf-8")
                    try:
                        report = run(client, text, tuple(args.target_box), expect_title=args.expect_title,
                                     caret_box=tuple(args.caret_box) if args.caret_box else None,
                                     prewarm=args.prewarm, light_status=not args.full_status,
                                     evidence=args.image_output, evidence_dir=args.evidence_dir)
                    except FlowStopped as stopped:
                        print(json.dumps({"error": str(stopped), **stopped.report.as_dict()},
                                         ensure_ascii=False), file=sys.stderr)
                        return 2
                    print(json.dumps(report.as_dict(), ensure_ascii=False))
                    return 0
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
                    if getattr(args, "include_observation", True) is False:
                        fields["include_observation"] = False
                    result = client.clipboard_stage(stage, **fields)
                case "point" | "tap" | "swipe" | "scroll" | "type" | "key" | "home" | "back" | \
                        "tap-text" | "compose-chinese" | "long-press" | "double-tap" | "circle" | "path" | \
                        "search-app":
                    fields = _action_fields(args)
                    name = "open_app" if args.command == "search-app" else args.command.replace("-", "_")
                    result = (client.control(name, args.screen_id, **fields) if args.screen_id
                              else client.action(name, **fields))
                case "calibrate": result = client.calibrate(*(args.pointer or []))
                case "middle-click-home":
                    if args.middle_action == "status":
                        result = client.middle_click_home_status()
                    elif args.middle_action == "enable":
                        result = client.enable_middle_click_home(args.screen_id)
                    else:
                        result = client.revoke_middle_click_home(all_devices=args.all)
                case "layouts": result = client.layouts()
                case "layout-tap": result = client.layout_tap(args.layout, args.point, args.screen_id)
                case "skill-list": result = client.skills()
                case "skill-show": result = client.skill(args.name)
                case "skill-check": result = client.skill_check(args.observed_text, args.skill)
                case "skill-prepare-send": result = client.skill_prepare(args.skill)
                case "skill-confirm-send":
                    result = client.skill_confirm(args.bubble_visible, args.observed_text, args.skill)
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
