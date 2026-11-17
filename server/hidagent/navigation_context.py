"""Detector-issued context for the system Back and AssistiveTouch Home controls.

Only these known navigation actions can ignore unrelated app content. A client
cannot submit regions or turn an ordinary tap/text action into a scoped action.
"""

import io
import math
from dataclasses import dataclass, field

from PIL import Image

from . import assistive_vision
from .freshness import FrameSnapshot, require_fresh
from .navigation_vision import BackTarget, find_back_target

_ISSUER = object()


def for_phone(phone, action, observation):
    """Honor the phone's configured Home layout without accepting client regions."""
    assistive = getattr(phone, "assistive_touch", None)
    make_context = getattr(assistive, "make_context", None)
    if action == "home" and make_context is not None:
        return make_context(observation)
    return from_navigation(action, observation)


@dataclass(frozen=True, init=False)
class NavigationContext:
    action: str
    kind: str
    observation: FrameSnapshot = field(repr=False)
    target: BackTarget
    regions: tuple[tuple[float, float, float, float], ...]
    _seal: object = field(repr=False)

    def __init__(self, *args, **kwargs):
        raise TypeError("navigation context must come from a visible system-control detector")

    def validate_target(self, frame, generation, center):
        """Bind a contact to this detected control, original pixels and session."""
        if (self._seal is not _ISSUER or frame != self.observation.frame
                or generation != self.observation.generation):
            raise RuntimeError("导航目标不属于原画面或画面连接已变化，未发送操作。")
        left, top, right, bottom = self.target.box
        if (len(center) != 2 or not all(math.isfinite(value) for value in center)
                or not left < center[0] < right or not top < center[1] < bottom):
            raise ValueError("navigation coordinates must belong to the detected system control")

    def validate_current(self, screen, *, cursor=None, reference=None):
        """Keep the full observed nav anchor, with only measured cursor exclusions."""
        from .text_target import TextTargetGuard

        current = require_fresh(screen)
        self.validate_target(self.observation.frame, current.generation, self.target.center)
        # Do not rerun shape recognition under the pointer: the measured cursor
        # can cover the arrow/icon. This guard retains every other anchor pixel.
        TextTargetGuard(screen, self.observation.frame, self.target,
                        observation=self.observation, navigation_context=self)(cursor=cursor, reference=reference)
        return current


def from_navigation(action, observation, *, home_slot=None, button_hint=None) -> NavigationContext | None:
    """Issue context only from an observed Back arrow or AssistiveTouch control.

    A configured Home slot/hint is merely a search hint: actual icon/bubble
    boundaries must still be detected. No externally chosen region is accepted.
    """
    if action not in {"back", "home"} or not isinstance(observation, FrameSnapshot) or not observation.frame:
        return None
    try:
        with Image.open(io.BytesIO(observation.frame)) as image:
            width, height = image.size
        if action == "back":
            target = find_back_target(observation.frame)
            if target is None:
                return None
            kind = "back"
            # Preserve the whole navigation bar (arrow, label and page title),
            # not just the small clickable rectangle. List scans below may vary.
            regions = ((0., 0., 1., min(1., max(.18, target.box[3] + .04))),)
        else:
            panel = assistive_vision.find_panel(observation.frame)
            if panel is not None:
                left, top, right, bottom = panel
                slot = home_slot or ((left + right) / 2, top + .82 * (bottom - top))
                detected = assistive_vision.find_icon_target(observation.frame, panel, slot)
                if detected is None:
                    return None
                kind, regions = "menu", (panel,)
            else:
                detected = assistive_vision.find_button_target(observation.frame, near=button_hint)
                if detected is None:
                    return None
                kind = "bubble"
                left, top, right, bottom = detected[1]
                margin_x, margin_y = .03, .03 * width / height
                regions = ((max(0., left-margin_x), max(0., top-margin_y),
                            min(1., right+margin_x), min(1., bottom+margin_y)),)
            target = BackTarget(*detected)
    except (OSError, ValueError):
        return None
    context = object.__new__(NavigationContext)
    for key, value in {"action": action, "kind": kind, "observation": observation,
                       "target": target, "regions": regions, "_seal": _ISSUER}.items():
        object.__setattr__(context, key, value)
    return context
