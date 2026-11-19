"""Repeatable host-side staged paste flow with per-stage timing. It never sends a message.

The flow only sequences the existing guarded endpoints (observe, click-observed,
paste prepare/status/paste/confirm). Every safety check stays on the server:
this module adds no masks or exemptions. It stops before any Send control.
"""

import base64
import io
import time
from dataclasses import dataclass, field

from PIL import Image, ImageChops

PRE_DISPATCH = ("未发送原地点击", "未发送点击", "等待点击期间原画面已变化")
ALLOW_LABELS = ("允许粘贴", "Allow Paste")
PASTE_ATTEMPTS = 3  # pre-dispatch 409 retries for the paste stage; Cmd+V is still sent at most once

# What a caller must do next for each terminal state. Never contains text or ids.
RECOVERY = {
    "completed": "Paste confirmed. Inspect the field before any Send.",
    "not_copied": "Nothing was copied or pasted. Inspect the screen, then run paste-flow again.",
    "copied_not_pasted": (
        "The Shortcut copied the text but Cmd+V was NOT sent; the clipboard stays blocked "
        "(needs_resolution). Do not rerun paste-flow, wait for TTL or guess ids. Confirm the Shortcut "
        "finished and the field is unchanged, then resolve per docs/PASTE_INPUT.md or restart the worker."),
    "claimed_not_confirmed": (
        "The Shortcut claimed the text but never confirmed the copy; Cmd+V was NOT sent and the clipboard "
        "stays blocked (needs_resolution). Do not rerun paste-flow, wait for TTL or guess ids. Stop the old "
        "Shortcut, check the field, then resolve per docs/PASTE_INPUT.md or restart the worker."),
    "paste_sent_unconfirmed": (
        "Cmd+V may have been sent. Never paste again or clear the field. Inspect the field and stop; "
        "the clipboard stays blocked until the operator checks the result and resolves per "
        "docs/PASTE_INPUT.md or restarts the worker."),
}


def _recovery(report, state):
    report.facts["recovery_state"] = state
    report.facts["next_action"] = RECOVERY[state]


class FlowStopped(RuntimeError):
    """The flow stopped before completion; `report` holds what already happened."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


@dataclass
class Report:
    stages: dict = field(default_factory=dict)
    server_timing_ms: dict = field(default_factory=dict)
    retries: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)

    def retry(self, name):
        self.retries[name] = self.retries.get(name, 0) + 1

    def as_dict(self):
        return {"stages_ms": {k: round(v, 1) for k, v in self.stages.items()},
                "server_timing_ms": self.server_timing_ms, "retries": self.retries, **self.facts}


class _Stage:
    def __init__(self, report, name):
        self.report, self.name = report, name

    def __enter__(self):
        self.began = time.perf_counter()

    def __exit__(self, *_):
        self.report.stages[self.name] = self.report.stages.get(self.name, 0) + (
            time.perf_counter() - self.began) * 1000


def _rgb(raw):
    with Image.open(io.BytesIO(raw)) as image:
        return image.convert("RGB")


def green_level(raw, box):
    """Strongest green-over-red tone inside a normalized box (WeChat caret phase)."""
    image = _rgb(raw)
    w, h = image.size
    left, top, right, bottom = (round(v * (w if i % 2 == 0 else h)) for i, v in enumerate(box))
    pixels = image.crop((left, top, max(left + 1, right), max(top + 1, bottom))).tobytes()
    return max((g - r for r, g in zip(pixels[0::3], pixels[1::3], strict=True)), default=0)


def wait_caret_plateau(client, box, *, high=150, low=12, timeout=3.0):
    """Wait for a fresh off->on caret edge, so the next ~0.4 s is visually stable.

    Returns False when no blinking green caret is found; callers then simply proceed.
    This is only scheduling: the server still rejects any changed frame.
    """
    deadline = time.monotonic() + timeout
    seen_low = False
    while time.monotonic() < deadline:
        level = green_level(client.screenshot(), box)
        if level <= low:
            seen_low = True
        elif level >= high and seen_low:
            return True
        time.sleep(.03)
    return False


def _observe(client, report, *, for_click=False, stage="observe"):
    with _Stage(report, stage):
        return client.observe(for_click=for_click)


def _field_lines(raw, box):
    from . import ocr
    left, top, right, bottom = box
    return [item.text for item in ocr.read_text(raw)
            if left <= item.box[0] and item.box[2] <= right and top <= item.box[1] and item.box[3] <= bottom]


def _stray_cursor_outside_bubble(before: bytes | None, after: bytes | None, point: tuple[float, float],
                                 box: tuple[float, float, float, float]) -> tuple[float, float] | None:
    """Locate an arriving grey pointer disc near (but outside) the AssistiveTouch box."""
    if not before or not after:
        return None
    try:
        with Image.open(io.BytesIO(before)) as first, Image.open(io.BytesIO(after)) as second:
            if first.size != second.size:
                return None
            w = min(360, first.width)
            h = max(1, round(first.height * w / first.width))
            dark = ImageChops.subtract(
                first.convert("L").resize((w, h), Image.Resampling.BILINEAR),
                second.convert("L").resize((w, h), Image.Resampling.BILINEAR),
            )
    except (OSError, ValueError):
        return None
    x0, x1 = max(0, round((box[0] - .12) * w)), min(w, round((box[2] + .06) * w))
    y0, y1 = max(round(.06 * h), round((box[1] - .12) * h)), min(h, round((box[3] + .12) * h))
    bx0, by0, bx1, by1 = box[0] - .008, box[1] - .008, box[2] + .008, box[3] + .008
    pixels = dark.load()
    pts = []
    for py in range(y0, y1):
        yn = py / h
        for px in range(x0, x1):
            if pixels[px, py] > 16:
                xn = px / w
                if not (bx0 <= xn <= bx1 and by0 <= yn <= by1):
                    pts.append((xn, yn))
    if not 40 <= len(pts) <= 600:
        return None
    sx = sum(px for px, _ in pts) / len(pts)
    sy = sum(py for _, py in pts) / len(pts)
    return (sx, sy) if abs(sx - point[0]) <= .10 and abs(sy - point[1]) <= .10 else None


def _hold(client, report, point, box, caret_box, stage):
    for _ in range(5):
        if caret_box:
            with _Stage(report, "caret_sync"):
                wait_caret_plateau(client, caret_box)
        observed = _observe(client, report, for_click=True, stage=f"{stage}_observe")
        with _Stage(report, stage):
            result = client.click_observed(observed["screen_id"], point[0], point[1], list(box),
                                           target_label="辅助触控", duration=.9)
        report.server_timing_ms.setdefault(stage, []).append(result.get("timing_ms"))
        if result.get("command_acknowledged") is True:
            return result
        if not any(marker in (result.get("error") or "") for marker in PRE_DISPATCH):
            raise RuntimeError(f"AssistiveTouch hold not acknowledged: {result.get('error')}")
        report.retry(stage)
    raise RuntimeError("AssistiveTouch hold kept changing before dispatch")


def run(client, text, target_box, *, expect_title=None, caret_box=None, prewarm=False,
        light_status=True, copy_timeout=20.0, confirm_attempts=4, evidence=None, evidence_dir=None):
    """Prepare -> copy -> one Cmd+V -> optional permission sheet -> confirm. Never sends."""
    from . import assistive_vision, ocr

    report = Report()
    began = time.perf_counter()

    def keep(name, result):
        if evidence_dir is not None and result and result.get("image_base64"):
            (evidence_dir / f"{name}.jpg").write_bytes(base64.b64decode(result["image_base64"]))
    transaction = None
    report.facts.update(copied=False, cmd_v_sent=0)
    try:
        trigger_configured = False
        if hasattr(client, "capabilities"):
            caps = client.capabilities() or {}
            trigger_configured = bool(((caps.get("features") or {}).get("clipboard") or {}).get("trigger_configured"))
        report.facts["trigger_configured"] = trigger_configured
        first = _observe(client, report, stage="initial_observe")
        frame = base64.b64decode(first["image_base64"])
        if expect_title:
            title = [item for item in ocr.read_text(frame) if item.center[1] < .12
                     and ocr.normalize_text(item.text) == ocr.normalize_text(expect_title)]
            if len(title) != 1:
                raise RuntimeError("conversation title does not match; nothing was pasted")
        draft = _field_lines(frame, target_box)
        if draft:
            report.facts["existing_draft_lines"] = len(draft)
            raise RuntimeError("input field already has a draft; it was kept and nothing was pasted")
        bubble = assistive_vision.find_button_target(frame)
        for _ in range(6):
            if bubble is not None:
                break
            time.sleep(.25)  # an idle, dimmed button may be missed in a single frame
            bubble = assistive_vision.find_button_target(client.screenshot())
        if bubble is None:
            raise RuntimeError("AssistiveTouch button not visible")
        point, box = bubble

        def point_to_bubble(aim_x, aim_y, *, rehome_flag):
            # The server requires target_box to contain the aim point. A
            # correction aim may sit outside the button by design; send the box
            # only when it contains the aim, and verify the landing afterwards.
            fields = {"target_box": list(box)} if box[0] < aim_x < box[2] and box[1] < aim_y < box[3] else {}
            if rehome_flag:
                fields["rehome"] = True
            for attempt in range(4):
                if caret_box:
                    with _Stage(report, "caret_sync"):
                        wait_caret_plateau(client, caret_box)
                observed = client.observe()
                try:
                    with _Stage(report, "point_bubble_action"):
                        try:
                            moved = client.control("point", observed["screen_id"], x=aim_x, y=aim_y, **fields)
                        except RuntimeError as exc:
                            if "422" not in str(exc) or not fields:
                                raise
                            fields.clear()  # older server: plain point without target_box/rehome
                            moved = client.control("point", observed["screen_id"], x=aim_x, y=aim_y)
                    report.server_timing_ms.setdefault("point_bubble", []).append(moved.get("timing_ms"))
                    return base64.b64decode(observed["image_base64"]) if observed.get("image_base64") else None
                except RuntimeError as exc:
                    if "409" not in str(exc) or attempt == 3:
                        raise
                    report.retry("point_bubble")
            return None

        with _Stage(report, "point_bubble"):
            before_point = point_to_bubble(point[0], point[1], rehome_flag=True)
        time.sleep(.8)
        after_point = client.screenshot()
        # The hovered button is opaque; refine its centre and bounds from a fresh frame.
        hovered = assistive_vision.find_button_target(after_point)
        if hovered is not None and box[0] < hovered[0][0] < box[2]:
            point, box = hovered
        stray = _stray_cursor_outside_bubble(before_point, after_point, point, box)
        if stray is not None:
            report.retry("point_bubble_miss")
            cx = min(.99, max(.01, point[0] + (point[0] - stray[0])))
            cy = min(.99, max(.01, point[1] + (point[1] - stray[1])))
            with _Stage(report, "point_bubble"):
                point_to_bubble(cx, cy, rehome_flag=False)
            time.sleep(.8)
            after_point = client.screenshot()
            hovered = assistive_vision.find_button_target(after_point)
            if hovered is not None and box[0] < hovered[0][0] < box[2]:
                point, box = hovered
            if _stray_cursor_outside_bubble(before_point, after_point, point, box) is not None:
                raise RuntimeError("pointer landed outside AssistiveTouch button; nothing was pasted")
        if prewarm:
            _hold(client, report, point, box, caret_box, "prewarm_hold")
            time.sleep(2.5)

        if caret_box:
            with _Stage(report, "caret_sync"):
                wait_caret_plateau(client, caret_box)
        observed = _observe(client, report, stage="prepare_observe")
        prepare_frame = base64.b64decode(observed["image_base64"]) if observed.get("image_base64") else None
        keep("1-prepare-original", observed)
        with _Stage(report, "prepare"):
            prepared = client.clipboard_stage("prepare", screen_id=observed["screen_id"], text=text,
                                              focused=True, target_box=list(target_box))
        transaction = prepared["transaction_id"]
        time.sleep(.6)
        fields = {"transaction_id": transaction}
        if light_status:
            fields["include_observation"] = False

        def poll_status():
            while True:
                try:
                    return client.clipboard_stage("status", **fields)
                except RuntimeError as exc:
                    if "422" not in str(exc) or "include_observation" not in fields:
                        raise
                    fields.pop("include_observation")  # older server: full status only
                    report.facts["light_status"] = False

        polls = 0
        copied = False
        if trigger_configured:
            with _Stage(report, "wait_copied"):
                state = poll_status()
                polls += 1
                if state.get("copied") and state.get("phase") == "awaiting_paste":
                    copied = True
                    report.facts["copy_trigger"] = "trigger_key"
        if not copied:
            _hold(client, report, point, box, caret_box, "shortcut_hold")
            with _Stage(report, "wait_copied"):
                deadline = time.monotonic() + copy_timeout
                while True:
                    state = poll_status()
                    polls += 1
                    if state.get("copied") and state.get("phase") == "awaiting_paste":
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("Shortcut did not acknowledge a copy")
                    time.sleep(.25)
            report.facts["copy_trigger"] = "assistive_touch_hold"
        report.facts["status_polls"] = polls
        report.facts["copied"] = True

        if prepare_frame:
            from . import screen_compare
            overlay_deadline = time.monotonic() + 2.5
            while time.monotonic() < overlay_deadline:
                if not screen_compare.observation_changed(
                        prepare_frame, client.screenshot(), caret_scope=target_box, allow_assistive_opacity=True):
                    break
                with _Stage(report, "wait_overlay_clear"):
                    time.sleep(.15)

        # Fresh frame after the copy ACK, then exactly one Cmd+V. A 409 from the
        # paste stage is raised by the server's pre-dispatch checks (ticket,
        # freshness, original-context guard) before Cmd+V is written; once the
        # key is attempted the server answers 200 with command_acknowledged.
        # So a 409 is safe to retry on a new observation, never a sent key.
        pasted = None
        for attempt in range(PASTE_ATTEMPTS):
            if caret_box:
                with _Stage(report, "caret_sync"):
                    wait_caret_plateau(client, caret_box)
            observed = _observe(client, report, stage="paste_observe")
            keep("2-paste-original" if attempt == 0 else f"2-paste-original-{attempt}", observed)
            try:
                with _Stage(report, "paste"):
                    pasted = client.clipboard_stage("paste", transaction_id=transaction,
                                                    screen_id=observed["screen_id"], focused=True)
                break
            except RuntimeError as exc:
                if "409" not in str(exc) or attempt == PASTE_ATTEMPTS - 1:
                    raise
                report.retry("paste")
                report.facts.setdefault("paste_rejections", []).append(str(exc))
                time.sleep(.35)
        keep("3-after-cmd-v", pasted)
        report.facts["cmd_v_sent"] = 1 if pasted.get("command_acknowledged") else 0
        if not pasted.get("command_acknowledged"):
            raise RuntimeError(f"Cmd+V was not acknowledged: {pasted.get('error')}")

        sheet = False
        with _Stage(report, "permission_sheet"):
            screen_id = pasted.get("screen_id")  # the post-Cmd+V evidence frame, when fresh
            for _ in range(4):
                with _Stage(report, "sheet_find"):
                    if screen_id is None:
                        screen_id = client.observe()["screen_id"]
                    found = None
                    for label in ALLOW_LABELS:
                        try:
                            hit = client.find_text(label, screen_id, exact=True)
                        except RuntimeError:
                            continue
                        if hit.get("match"):
                            found = hit["match"]
                            break
                if found is None:
                    screen_id = None
                    time.sleep(.3)
                    continue
                sheet = True
                (x, y), (left, top, right, bottom) = found["center"], found["box"]
                allow_box = [max(0, left - .005), max(0, top - .005), min(1, right + .005), min(1, bottom + .005)]
                with _Stage(report, "allow_point"):
                    moved = client.control("point", screen_id, x=x, y=y)
                report.server_timing_ms.setdefault("allow_point", []).append(moved.get("timing_ms"))
                time.sleep(.8)
                with _Stage(report, "allow_click"):
                    for _ in range(3):
                        clicking = client.observe(for_click=True)
                        result = client.click_observed(clicking["screen_id"], x, y, allow_box,
                                                       target_label=found["text"])
                        report.server_timing_ms.setdefault("allow_click", []).append(result.get("timing_ms"))
                        if result.get("command_acknowledged"):
                            break
                        report.retry("allow_paste_click")
                    else:
                        raise RuntimeError("could not press Allow Paste")
                break
        report.facts["permission_sheet_seen"] = sheet

        with _Stage(report, "confirm"):
            if sheet and caret_box:
                # Let the post-sheet field accumulate one caret blink cycle and
                # start confirm on a steady on-plateau.
                with _Stage(report, "caret_sync"):
                    wait_caret_plateau(client, target_box, timeout=1.5)
            for attempt in range(confirm_attempts):
                observed = client.observe()
                keep(f"4-confirm-{attempt}", observed)
                try:
                    receipt = client.clipboard_stage("confirm", transaction_id=transaction,
                                                     screen_id=observed["screen_id"],
                                                     observed_text=text, confirmed=True)
                    break
                except RuntimeError as exc:
                    if "409" not in str(exc) or attempt == confirm_attempts - 1:
                        report.facts["confirm_error"] = str(exc)
                        raise
                    report.retry("confirm")
                    report.facts.setdefault("confirm_rejections", []).append(str(exc))
                    time.sleep(.3)
        report.facts["phase"] = receipt.get("phase")
        report.facts["effect_verified"] = receipt.get("effect_verified")
        report.facts["confirmation_evidence"] = receipt.get("confirmation_evidence")
        transaction = None
        if evidence is not None and receipt.get("image_base64"):
            evidence.write_bytes(base64.b64decode(receipt["image_base64"]))
        _recovery(report, "completed")
        return report
    except Exception as exc:
        report.facts["stopped"] = str(exc)
        if transaction is not None and not report.facts.get("cmd_v_sent"):
            # Cmd+V never went out: cancelling is safe. A fetched copy still needs
            # the host to check the Shortcut and resolve, as the server requires.
            try:
                report.facts["cancelled"] = client.clipboard_stage("cancel", transaction_id=transaction)
                transaction = None
            except RuntimeError as cancel_error:
                report.facts["cancel_error"] = str(cancel_error)
        report.facts["transaction_left_open"] = transaction is not None
        if report.facts.get("cmd_v_sent"):
            _recovery(report, "paste_sent_unconfirmed")
        elif report.facts.get("copied"):
            _recovery(report, "copied_not_pasted")
        elif (report.facts.get("cancelled") or {}).get("needs_resolution") or "cancel_error" in report.facts:
            _recovery(report, "claimed_not_confirmed")
        else:
            _recovery(report, "not_copied")
        raise FlowStopped(str(exc), report) from exc
    finally:
        report.stages["total"] = (time.perf_counter() - began) * 1000
