"""Repeatable host-side staged paste flow with per-stage timing. It never sends a message.

The flow only sequences the existing guarded endpoints (observe, click-observed,
paste prepare/status/paste/confirm). Every safety check stays on the server:
this module adds no masks or exemptions. It stops before any Send control.
"""

import base64
import io
import math
import re
import time
from dataclasses import dataclass, field
from typing import NamedTuple

import httpx
from PIL import Image

from . import assistive_vision

PRE_DISPATCH = ("未发送原地点击", "未发送点击", "等待点击期间原画面已变化")
ALLOW_LABELS = {"允许粘贴", "允許貼上", "allowpaste"}
ALLOW_MODAL_REGION = (0.13, 0.27, 0.87, 0.75)
PASTE_ATTEMPTS = 3  # pre-dispatch 409 retries for the paste stage; Cmd+V is still sent at most once
ALLOW_POINT_ATTEMPTS = 3
ALLOW_CHECK_ATTEMPTS = 4

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


def _field_lines_from_boxes(boxes, box):
    left, top, right, bottom = box
    return [item.text for item in boxes
            if left <= item.box[0] and item.box[2] <= right and top <= item.box[1] and item.box[3] <= bottom]


def _field_lines(raw, box):
    from . import ocr
    return _field_lines_from_boxes(ocr.read_text(raw), box)


def _verify_target_context(frame, target_box, *, expect_title=None, report=None):
    from . import ocr

    boxes = ocr.read_text(frame)
    if expect_title:
        title = [item for item in boxes if item.center[1] < .12
                 and ocr.normalize_text(item.text) == ocr.normalize_text(expect_title)]
        if len(title) != 1:
            raise RuntimeError("conversation title does not match; nothing was pasted")
    draft = _field_lines_from_boxes(boxes, target_box)
    if draft:
        if report is not None:
            report.facts["existing_draft_lines"] = len(draft)
        raise RuntimeError("input field already has a draft; it was kept and nothing was pasted")


def _center_field_lines(boxes, region):
    from . import paste_confirmation

    left, top, right, bottom = region
    inside = []
    for item in boxes:
        x0, y0, x1, y1 = item.box
        if left <= item.center[0] <= right and top <= item.center[1] <= bottom:
            if item.conf < paste_confirmation._MIN_CONFIDENCE:
                return None
            if paste_confirmation._visible_text(item.text):
                inside.append(item)
        elif x0 < right and x1 > left and y0 < bottom and y1 > top:
            # Allow slight 1-2px border grazing from adjacent text, but reject
            # boxes that substantially intrude across the field boundary.
            box_area = max(1e-6, (x1 - x0) * (y1 - y0))
            ov_area = max(0.0, min(x1, right) - max(x0, left)) * max(0.0, min(y1, bottom) - max(y0, top))
            if ov_area / box_area > 0.25:
                return None
    rows = []
    for item in sorted(inside, key=lambda item: item.center[1]):
        height = max(1e-4, item.box[3] - item.box[1])
        if not rows or abs(rows[-1][0].center[1] - item.center[1]) > height / 2:
            rows.append([])
        rows[-1].append(item)
    return tuple(
        paste_confirmation._visible_text("".join(item.text for item in sorted(row, key=lambda item: item.center[0])))
        for row in rows
    )


def _matches_scrolled_suffix(observed: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    """Allow tail-window match when a long paste scrolls past the visible input height."""
    from . import paste_confirmation

    if not observed or not expected:
        return False
    joined = "".join(observed)
    total_expected = sum(len(line) for line in expected)
    if len(joined) >= total_expected:
        return False
    if len(expected) == 1:
        return len(expected[0]) >= 30 and len(joined) >= 12 and expected[0].endswith(joined)
    if len(observed) < 2 or len(joined) < 8:
        return False
    if len(joined) >= 12 and expected[-1].endswith(joined):
        return True
    for k in range(1, len(expected)):
        tail = expected[k:]
        if paste_confirmation._matches_wrapped_lines(observed, tail):
            return True
        if (expected[k - 1].endswith(observed[0])
                and paste_confirmation._matches_wrapped_lines(observed[1:], tail)):
            return True
    return False


def _verify_confirm_frame(prepare_frame, confirm_frame, target_box, text, *, expect_title=None):
    from . import ocr, paste_confirmation, screen_compare

    boxes = ocr.read_text(confirm_frame)
    if expect_title:
        title = [item for item in boxes if item.center[1] < .12
                 and ocr.normalize_text(item.text) == ocr.normalize_text(expect_title)]
        if len(title) != 1:
            return False, "conversation title changed before confirmation"
    expected = paste_confirmation._expected_lines(text)
    if not expected:
        return False, "pasted text has no visible characters to visually verify"
    regions = []
    if prepare_frame:
        try:
            region = tuple(target_box)
            suggestion_bar = paste_confirmation._located_suggestion_bar(prepare_frame, confirm_frame, region)
            if suggestion_bar is not None:
                assistive_box = screen_compare.stationary_assistive_button_box(
                    prepare_frame, confirm_frame, exclude=(region,))
                grown = paste_confirmation._grown_field(
                    _rgb(prepare_frame), _rgb(confirm_frame), region, suggestion_bar,
                    assistive_box=assistive_box)
                if grown is not None:
                    regions.append(grown[0])
        except (OSError, ValueError):
            pass
    regions.append(tuple(target_box))
    for region in regions:
        strict_lines = paste_confirmation._field_lines(boxes, region)
        if strict_lines is not None and paste_confirmation._matches_wrapped_lines(strict_lines, expected):
            return True, "full"
        relaxed_lines = _center_field_lines(boxes, region)
        if relaxed_lines is not None and paste_confirmation._matches_wrapped_lines(relaxed_lines, expected):
            return True, "full"
    for region in regions:
        strict_lines = paste_confirmation._field_lines(boxes, region)
        if strict_lines is not None and _matches_scrolled_suffix(strict_lines, expected):
            return True, "suffix"
        relaxed_lines = _center_field_lines(boxes, region)
        if relaxed_lines is not None and _matches_scrolled_suffix(relaxed_lines, expected):
            return True, "suffix"
    return False, "pasted text was not observed in target field"


def _pointer_in_box(moved: dict | None, box) -> tuple[float, float] | None:
    mp = (moved or {}).get("measured_pointer")
    if isinstance(mp, dict):
        mx, my = mp.get("x"), mp.get("y")
        if (isinstance(mx, (int, float)) and not isinstance(mx, bool)
                and isinstance(my, (int, float)) and not isinstance(my, bool)
                and box[0] < mx < box[2] and box[1] < my < box[3]):
            return float(mx), float(my)
    return None


def _frame(observation):
    if not observation.get("screen_id") or not observation.get("image_base64"):
        raise RuntimeError("AssistiveTouch verification requires a fresh observation")
    return base64.b64decode(observation["image_base64"])


def _published(observation):
    value = observation.get("frame_received_at")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise RuntimeError("AssistiveTouch observation has no publication time")
    return value


class _MeasuredPointer(NamedTuple):
    id: str
    point: tuple[float, float]


def _measurement(observation):
    token = observation.get("pointer_measurement_id")
    point = _pointer_in_box(observation, (0, 0, 1, 1))
    if observation.get("pointer_seen") is not True or not isinstance(token, str) or not token or point is None:
        raise RuntimeError("AssistiveTouch pointer landing was not confirmed")
    return _MeasuredPointer(token, point)


def _inside(point, box):
    return box[0] < point[0] < box[2] and box[1] < point[1] < box[3]


class _BubblePointer:
    """One acquisition budget, from pointing to a verified prepare/hold."""

    def __init__(self, client, report, caret_box, field_box, check_context):
        self.client = client
        self.report = report
        self.caret_box = caret_box
        self.caret_scope = list(field_box) if caret_box else None
        self.check_context = check_context
        self.attempts = 0
        self.target = None
        self.proof = None
        self.published = None

    def sync_caret(self):
        if self.caret_box:
            with _Stage(self.report, "caret_sync"):
                wait_caret_plateau(self.client, self.caret_box)

    def _target_on(self, observed):
        target = assistive_vision.track_button_target(_frame(observed), previous=self.target)
        if target is None:
            raise RuntimeError("AssistiveTouch button not visible")
        if self.target is not None and target.size != self.target.size:
            raise RuntimeError("AssistiveTouch observation geometry changed")
        return target

    def _move(self, correction):
        legacy = False
        for attempt in range(4):
            self.sync_caret()
            observed = _observe(self.client, self.report)
            self.check_context(_frame(observed))
            current = self._target_on(observed)
            aim = current.point
            args = {"measure_only": True}
            if self.caret_scope is not None:
                args["caret_scope"] = self.caret_scope
            if not legacy:
                args["target_box"] = list(current.probe_box)
            if correction is not None:
                previous, measurement = correction
                if assistive_vision.same_button(previous, current):
                    if _measurement(observed) != measurement:
                        raise RuntimeError("AssistiveTouch correction lost its measured landing")
                    args["correct_from_measurement_id"] = measurement.id
            self.target = current
            if self.attempts == 1 and not legacy:
                args["rehome"] = True
            try:
                with _Stage(self.report, "point_bubble_action"):
                    moved = self.client.control("point", observed["screen_id"], x=aim[0], y=aim[1], **args)
                self.report.server_timing_ms.setdefault("point_bubble", []).append(moved.get("timing_ms"))
                return moved, "relative" if "correct_from_measurement_id" in args else "absolute"
            except RuntimeError as exc:
                if str(exc).startswith("HTTP 422:") and not legacy and correction is None:
                    legacy = True
                elif not str(exc).startswith("HTTP 409:"):
                    raise
                if attempt == 3:
                    raise
                self.report.retry("point_bubble")

    def _next_publication(self, moved):
        # Two different screen tickets may wrap the same cached publication.
        published = _published(moved)
        for _ in range(6):
            observed = _observe(self.client, self.report, stage="point_bubble_verify")
            if observed.get("screen_id") != moved["screen_id"] and _published(observed) > published:
                return observed
            time.sleep(.12)
        raise RuntimeError("AssistiveTouch pointer has no independent post-move frame")

    def _verify_move(self, moved, entry):
        """Return a corroborated miss for one correction, or discard a relocated target."""
        if moved.get("command_acknowledged") is not True or moved.get("fresh") is not True:
            raise RuntimeError("AssistiveTouch move did not produce a fresh acknowledged observation")
        aimed = self.target
        self.target = self._target_on(moved)
        if assistive_vision.same_button(aimed, self.target):
            measured = _measurement(moved)
            entry["measured"] = measured.point
            observed = self._next_publication(moved)
            self.target = self._target_on(observed)
            if assistive_vision.same_button(aimed, self.target):
                if _measurement(observed) != measured:
                    raise RuntimeError("AssistiveTouch pointer measurement changed after the move")
                entry["corroborated"] = True
                if _inside(measured.point, self.target.safe_box):
                    entry["outcome"] = "verified"
                    self.proof, self.published = measured, _published(observed)
                    return None
                entry["outcome"] = "outside"
                return self.target, measured
        entry["outcome"] = "button_moved"
        return None

    def acquire(self):
        correction = None
        self.proof = None
        while self.attempts < 2:
            self.attempts += 1
            with _Stage(self.report, "point_bubble"):
                moved, mode = self._move(correction)
                entry = {"target": self.target.point, "safe_box": self.target.safe_box,
                         "probe_box": self.target.probe_box, "aim": self.target.point, "mode": mode,
                         "measured": None, "corroborated": False, "outcome": "unconfirmed"}
                self.report.facts.setdefault("bubble_attempts", []).append(entry)
                correction = self._verify_move(moved, entry)
            if self.proof is not None:
                return
            if self.attempts < 2:
                self.report.retry("point_bubble_miss")
        raise RuntimeError("pointer landed outside AssistiveTouch button or target moved; nothing was pasted")

    def verified_observation(self, stage, *, for_click=False):
        self.sync_caret()
        observed = _observe(self.client, self.report, for_click=for_click, stage=stage)
        if for_click and observed.get("for_click") is not True:
            raise RuntimeError("AssistiveTouch hold requires a qualified for_click observation")
        self.check_context(_frame(observed))
        current = self._target_on(observed)
        if not assistive_vision.same_button(self.target, current):
            if for_click or self.attempts == 2:
                raise RuntimeError("AssistiveTouch button moved before dispatch")
            self.report.retry("point_bubble_miss")
            self.target = current
            self.acquire()
            return self.verified_observation(stage)
        if (_published(observed) < self.published or _measurement(observed) != self.proof
                or not _inside(self.proof.point, current.safe_box)):
            raise RuntimeError("AssistiveTouch pointer is no longer verified in the current button")
        self.target = current
        return observed


def _hold(client, report, landing, stage):
    for _ in range(5):
        observed = landing.verified_observation(f"{stage}_observe", for_click=True)
        scope = {"caret_scope": landing.caret_scope} if landing.caret_scope is not None else {}
        try:
            with _Stage(report, stage):
                result = client.click_observed(
                    observed["screen_id"], *landing.proof.point, list(landing.target.safe_box),
                    target_label="辅助触控", duration=.9, **scope)
        except (RuntimeError, OSError, httpx.HTTPError) as exc:
            # A cancelled host-click can return HTTP 409 after the press write.
            if not (isinstance(exc, httpx.ConnectError) or str(exc).startswith("HTTP 422:")):
                report.facts["hold_may_have_executed"] = True
            raise
        report.server_timing_ms.setdefault(stage, []).append(result.get("timing_ms"))
        if result.get("command_acknowledged") is True:
            return result
        if result.get("action_may_have_executed") is not False or not any(
                marker in (result.get("error") or "") for marker in PRE_DISPATCH):
            report.facts["hold_may_have_executed"] = True
            raise RuntimeError(f"AssistiveTouch hold not acknowledged: {result.get('error')}")
        report.retry(stage)
    raise RuntimeError("AssistiveTouch hold kept changing before dispatch")


def _page_header(items):
    from . import ocr, paste_confirmation

    return tuple((ocr.normalize_text(item.text), item.center) for item in sorted(items, key=lambda it: it.center[0])
                 if .055 < item.center[1] < .14 and item.conf >= paste_confirmation._MIN_CONFIDENCE)


def _on_prepared_page(prepared, current, expect_title):
    from . import ocr

    wanted = ocr.normalize_text(expect_title) if expect_title else None
    for label, center in prepared:
        # With no caller-provided title, use a distinctive left/centre heading
        # from the actual prepare frame; generic navigation controls recur across apps.
        if wanted:
            if label != wanted:
                continue
        elif (center[0] > .8 or len(label) < 3
              or label in {"back", "done", "edit", "cancel", "search", "more", "save", "返回", "完成", "取消"}):
            continue
        if any(label == text and math.dist(center, position) < .045 for text, position in current):
            return True
    return False


class _AllowTarget(NamedTuple):
    prompt: str
    header: tuple
    label: str
    center: tuple[float, float]
    box: tuple[float, float, float, float]
    deny_center: tuple[float, float]

    def same_sheet(self, other):
        from . import ocr

        if other is None:
            return False
        return (self.prompt == other.prompt and len(self.header) == len(other.header)
                and all(a == b and math.dist(p, q) < .045
                        for (a, p), (b, q) in zip(self.header, other.header, strict=True))
                and ocr.normalize_text(self.label) == ocr.normalize_text(other.label)
                and _inside(other.center, self.box) and _inside(self.center, other.box)
                and math.dist(self.deny_center, other.deny_center) < .025)


def _allow_target(frame):
    from . import ocr, paste_confirmation

    items = ocr.read_text(frame)
    prompt = paste_confirmation.permission_prompt(items)
    if prompt is None:
        return None
    description, allow, deny = prompt
    left, top, right, bottom = allow.box
    x0, y0, x1, y1 = ALLOW_MODAL_REGION
    if not (x0 < left < right < x1 and y0 < top < bottom < y1
            and _inside(allow.center, allow.box) and _inside(deny.center, deny.box)):
        raise RuntimeError("Allow Paste button is outside the permission prompt")
    box = (max(x0, left - .02), max(y0, top - .012), min(x1, right + .02), min(y1, bottom + .012))
    if box[0] < deny.box[2] and deny.box[0] < box[2] and box[1] < deny.box[3] and deny.box[1] < box[3]:
        raise RuntimeError("Allow and Deny Paste buttons overlap")
    return _AllowTarget(description, _page_header(items), allow.text, allow.center, box, deny.center)


def _stale_permission_ticket(exc):
    reason = str(exc)
    return reason.startswith("HTTP 409:") and any(marker in reason for marker in (
        "screen changed since", "screen_id expired", "invalid or consumed screen_id"))


def _next_allow_observation(client, report, moved, *, for_click=False):
    """Wait passively for a distinct publication of this move."""
    stage = "allow_click_observe" if for_click else "allow_correction_observe"
    for attempt in range(ALLOW_CHECK_ATTEMPTS):
        try:
            observed = _observe(client, report, for_click=for_click, stage=stage)
        except RuntimeError as exc:
            if not _stale_permission_ticket(exc):
                raise
        else:
            at, after = _published(moved), _published(observed)
            if after < at or (after == at and _frame(moved) != _frame(observed)):
                raise RuntimeError("Allow Paste observation publication changed inconsistently")
            if after > at and observed["screen_id"] != moved["screen_id"]:
                return observed
        if attempt + 1 < ALLOW_CHECK_ATTEMPTS:
            report.retry("allow_paste_publication")
            time.sleep(.12)
    raise RuntimeError("Allow Paste pointer has no independent post-move frame")


def _press_allow(client, report, expected, transaction):
    """A move may be repeated; dispatch at most one acknowledged permission click."""
    from . import ocr

    if ocr.normalize_text(expected.label) not in ALLOW_LABELS:
        raise RuntimeError("no explicit Allow Paste button in the permission prompt")
    # This flow stages through Shortcuts, never an arbitrary clipboard source.
    source = expected.prompt.translate(str.maketrans("", "", '“”「」\"'))
    if not re.search(r"(?:[从從](?:快捷指令|捷徑)(?:粘贴|貼上)|pastefromshortcuts(?:$|[^a-z]))", source):
        raise RuntimeError("paste permission prompt changed or has an unexpected source")
    miss = None
    attempt = stale_attempts = 0
    while attempt < ALLOW_POINT_ATTEMPTS:
        observed = (_next_allow_observation(client, report, miss[0]) if miss
                    else _observe(client, report, stage="allow_observe"))
        target = _allow_target(_frame(observed))
        if not expected.same_sheet(target):
            raise RuntimeError("paste permission prompt changed before pointing")
        fields = {"x": target.center[0], "y": target.center[1], "target_box": list(target.box), "measure_only": True}
        if miss and _measurement(observed) != miss[1]:
            raise RuntimeError("Allow Paste pointer measurement changed before re-aiming")
        if attempt:
            # Relative HID correction also guards the original AssistiveTouch target.
            # For this system button, acquire and measure a new absolute point.
            fields["rehome"] = True
        miss = None
        try:
            with _Stage(report, "allow_point"):
                moved = client.control("point", observed["screen_id"], **fields)
        except RuntimeError as exc:
            # Rejected before point dispatch: refresh and recheck the same sheet.
            # A stale ticket must not consume the independent pointer-correction
            # budget (the recorded failure had already used two measurements).
            stale_attempts += 1
            if not _stale_permission_ticket(exc) or stale_attempts >= ALLOW_POINT_ATTEMPTS:
                raise
            report.retry("allow_paste_point")
            continue
        attempt += 1
        report.server_timing_ms.setdefault("allow_point", []).append(moved.get("timing_ms"))
        if moved.get("command_acknowledged") is not True:
            raise RuntimeError("Allow Paste move was not acknowledged")
        if moved.get("fresh") is not True or moved.get("pointer_seen") is not True:
            report.retry("allow_paste_point")
            continue
        measured = _measurement(moved)
        if not _inside(measured.point, target.box):
            miss = (moved, measured)
            report.retry("allow_paste_point")
            continue
        clicking = _next_allow_observation(client, report, moved, for_click=True)
        current = _allow_target(_frame(clicking))
        if not expected.same_sheet(current) or not target.same_sheet(current):
            raise RuntimeError("paste permission prompt changed before clicking")
        if (clicking.get("for_click") is not True
                or _measurement(clicking) != measured or not _inside(measured.point, current.box)):
            raise RuntimeError("Allow Paste pointer was not confirmed on the current button")
        # This request may have been cancelled or expired while the
        # pointer was being measured. A light read preserves the click ticket.
        state = client.clipboard_stage("status", transaction_id=transaction, include_observation=False)
        ttl = state.get("expires_in_seconds")
        if (state.get("transaction_id") != transaction or state.get("phase") != "awaiting_confirmation"
                or state.get("copied") is not True or state.get("paste_sent") is not True
                or type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0):
            raise RuntimeError("paste transaction is no longer awaiting permission and confirmation")
        with _Stage(report, "allow_click"):
            result = client.click_observed(clicking["screen_id"], *measured.point, list(current.box),
                                           target_label=current.label)
        report.server_timing_ms.setdefault("allow_click", []).append(result.get("timing_ms"))
        if result.get("command_acknowledged") is True:
            return result
        error = (result.get("error") or "").lower()
        if (result.get("action_may_have_executed") is not False or not any(reason in error for reason in PRE_DISPATCH)
                or any(reason in error for reason in ("cancel", "abort", "取消", "急停", "已停止", "紧急停止"))):
            raise RuntimeError("Allow Paste click was not acknowledged; dispatch is uncertain")
        report.retry("allow_paste_click")
    raise RuntimeError("Allow Paste pointer or click was not confirmed")


def run(client, text, target_box, *, expect_title=None, caret_box=None, prewarm=False,
        light_status=True, copy_timeout=20.0, confirm_attempts=4, evidence=None, evidence_dir=None):
    """Prepare -> copy -> one Cmd+V -> optional permission sheet -> confirm. Never sends."""
    report = Report()
    began = time.perf_counter()

    def keep(name, result):
        if evidence_dir is not None and result and result.get("image_base64"):
            (evidence_dir / f"{name}.jpg").write_bytes(base64.b64decode(result["image_base64"]))
    transaction = None
    report.facts.update(copied=False, cmd_v_sent=0, action_may_have_executed=False)
    try:
        caps = client.capabilities() if hasattr(client, "capabilities") else {}
        features = (caps or {}).get("features") or {}
        trigger_configured = bool((features.get("clipboard") or {}).get("trigger_configured"))
        report.facts["trigger_configured"] = trigger_configured
        # Legacy action schemas ignore unknown fields and can execute a normal
        # calibrated point. Negotiate before sending the first positioning input.
        if not trigger_configured and features.get("point_measurement") is not True:
            raise RuntimeError("paste-flow requires a control server with point_measurement support")
        first = _observe(client, report, stage="initial_observe")
        frame = base64.b64decode(first["image_base64"])
        _verify_target_context(frame, target_box, expect_title=expect_title, report=report)

        def check_context(raw):
            if raw != frame:
                _verify_target_context(raw, target_box, expect_title=expect_title, report=report)

        if trigger_configured:
            for attempt in range(3):
                if attempt > 0:
                    time.sleep(.25)
                if caret_box:
                    with _Stage(report, "caret_sync"):
                        wait_caret_plateau(client, caret_box)
                if attempt > 0 or caret_box:
                    observed = _observe(client, report, stage="prepare_observe")
                    check_context(_frame(observed))
                else:
                    observed = first
                prepare_frame = _frame(observed)
                keep("1-prepare-original", observed)
                try:
                    with _Stage(report, "prepare"):
                        prepared = client.clipboard_stage("prepare", screen_id=observed["screen_id"], text=text,
                                                          focused=True, target_box=list(target_box))
                    break
                except RuntimeError as exc:
                    if not str(exc).startswith("HTTP 409:") or attempt == 2:
                        raise
                    report.retry("prepare")
        else:
            landing = _BubblePointer(client, report, caret_box, target_box, check_context)
            landing.target = assistive_vision.track_button_target(frame)
            for _ in range(6):
                if landing.target is not None:
                    break
                time.sleep(.25)
                landing.target = assistive_vision.track_button_target(client.screenshot())
            if landing.target is None:
                raise RuntimeError("AssistiveTouch button not visible")

            landing.acquire()
            if prewarm:
                _hold(client, report, landing, "prewarm_hold")
                time.sleep(2.5)
                # A hold is a new input. Acquire independent evidence for the
                # formal copy trigger even when the ball appears stationary.
                landing = _BubblePointer(client, report, caret_box, target_box, check_context)
                landing.acquire()
            observed = landing.verified_observation("prepare_observe")
            prepare_frame = _frame(observed)
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

        state = poll_status()
        polls = 1
        if trigger_configured:
            trigger = "trigger_key"
        elif state.get("claimed") or state.get("copied"):
            trigger = "already_claimed"
        else:
            _hold(client, report, landing, "shortcut_hold")
            trigger = "assistive_touch_hold"
            state = poll_status()
            polls += 1
        with _Stage(report, "wait_copied"):
            deadline = time.monotonic() + copy_timeout
            while not (state.get("copied") and state.get("phase") == "awaiting_paste"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Shortcut did not acknowledge a copy")
                time.sleep(.25)
                state = poll_status()
                polls += 1
        report.facts["copy_trigger"] = trigger
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
            except Exception as exc:
                msg = str(exc)
                is_connect_refused = isinstance(exc, httpx.ConnectError)
                is_pre_dispatch_409 = isinstance(exc, RuntimeError) and msg.startswith("HTTP 409:")
                is_pre_dispatch_422 = isinstance(exc, RuntimeError) and msg.startswith("HTTP 422:")
                if not (is_pre_dispatch_409 or is_pre_dispatch_422 or is_connect_refused):
                    report.facts["action_may_have_executed"] = True
                    raise
                if not is_pre_dispatch_409 or attempt == PASTE_ATTEMPTS - 1:
                    raise
                report.retry("paste")
                report.facts.setdefault("paste_rejections", []).append(msg)
                time.sleep(.35)
        report.facts["cmd_v_sent"] = 1 if pasted.get("command_acknowledged") else 0
        if pasted.get("action_may_have_executed"):
            report.facts["action_may_have_executed"] = True
        keep("3-after-cmd-v", pasted)
        if not pasted.get("command_acknowledged"):
            raise RuntimeError(f"Cmd+V was not acknowledged: {pasted.get('error')}")

        sheet = False
        with _Stage(report, "permission_sheet"):
            # Post-Cmd+V evidence can precede the settled sheet. It is only for
            # detection; point and click each use a newly observed ticket.
            observed = pasted
            for attempt in range(ALLOW_CHECK_ATTEMPTS):
                if not observed.get("image_base64") or not observed.get("screen_id"):
                    observed = client.observe()
                with _Stage(report, "sheet_find"):
                    candidate = _frame(observed)
                    found = _allow_target(candidate)
                if found is not None:
                    # The pasted text can itself contain permission labels. When
                    # it is already visible in the original field, only confirm.
                    if _verify_confirm_frame(prepare_frame, candidate, target_box, text, expect_title=expect_title)[0]:
                        break
                    from . import ocr
                    if not _on_prepared_page(_page_header(ocr.read_text(prepare_frame)), found.header, expect_title):
                        raise RuntimeError("paste permission prompt is not on the verified original page")
                    sheet = True
                    report.facts.update(permission_sheet_seen=True, permission_sheet_dismissed=False)
                    clicked = _press_allow(client, report, found, transaction)
                    keep("3b-allow-click", clicked)
                    # An ACK is already a dispatch. Only observe now, even if
                    # iOS keeps the permission sheet visible during animation.
                    for check in range(ALLOW_CHECK_ATTEMPTS):
                        post = client.observe()
                        raw = _frame(post)
                        keep(f"3b-allow-check-{check}", post)
                        remaining = _allow_target(raw)
                        if remaining is not None and not found.same_sheet(remaining):
                            raise RuntimeError("paste permission prompt changed after clicking")
                        if remaining is None and _verify_confirm_frame(
                                prepare_frame, raw, target_box, text, expect_title=expect_title)[0]:
                            report.facts["permission_sheet_dismissed"] = True
                            break
                        if check + 1 < ALLOW_CHECK_ATTEMPTS:
                            time.sleep(.2)
                    else:
                        raise RuntimeError("Allow Paste permission sheet did not dismiss with the expected text")
                    break
                if _verify_confirm_frame(prepare_frame, candidate, target_box, text, expect_title=expect_title)[0]:
                    break
                if attempt + 1 < ALLOW_CHECK_ATTEMPTS:
                    time.sleep(.3)
                    observed = {}
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
                confirm_frame = base64.b64decode(observed["image_base64"]) if observed.get("image_base64") else None
                if confirm_frame:
                    ok_text, detail = _verify_confirm_frame(
                        prepare_frame, confirm_frame, target_box, text, expect_title=expect_title)
                    if not ok_text:
                        report.facts.setdefault("confirm_rejections", []).append(detail)
                        if attempt == confirm_attempts - 1:
                            report.facts["confirm_error"] = detail
                            raise RuntimeError(detail)
                        report.retry("confirm_text")
                        time.sleep(.3)
                        continue
                    report.facts["client_text_verified"] = True
                    report.facts["client_text_match"] = detail
                else:
                    reason = "confirmation observation omitted image_base64"
                    report.facts.setdefault("confirm_rejections", []).append(reason)
                    if attempt == confirm_attempts - 1:
                        report.facts["confirm_error"] = reason
                        raise RuntimeError(reason)
                    report.retry("confirm_text")
                    time.sleep(.3)
                    continue
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
        paste_uncertain = bool(report.facts.get("cmd_v_sent") or report.facts.get("action_may_have_executed"))
        if transaction is not None and not paste_uncertain:
            # Cmd+V never went out: cancelling is safe. A fetched copy still needs
            # the host to check the Shortcut and resolve, as the server requires.
            try:
                report.facts["cancelled"] = client.clipboard_stage("cancel", transaction_id=transaction)
                transaction = None
            except Exception as cancel_error:
                report.facts["cancel_error"] = str(cancel_error)
        report.facts["transaction_left_open"] = transaction is not None
        if paste_uncertain:
            _recovery(report, "paste_sent_unconfirmed")
        elif report.facts.get("copied"):
            _recovery(report, "copied_not_pasted")
        elif (report.facts.get("cancelled") or {}).get("needs_resolution") or "cancel_error" in report.facts:
            _recovery(report, "claimed_not_confirmed")
        else:
            _recovery(report, "not_copied")
        if report.facts.get("hold_may_have_executed"):
            report.facts["next_action"] = (
                "The AssistiveTouch hold may have been dispatched. Inspect the Shortcut and clipboard "
                "state before any further input. Do not automatically repeat the hold or paste.")
        raise FlowStopped(str(exc), report) from exc
    finally:
        report.stages["total"] = (time.perf_counter() - began) * 1000
