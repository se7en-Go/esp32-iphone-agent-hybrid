"""Keep typing in its observed field, including while firmware ACKs are pending."""

from . import screen_compare
from .caret import focused_region as _focused_region
from .caret import temporal_focused_region
from .freshness import require_fresh


class InputContextGuard:
    def __init__(self, screen, observation, *, allow_white=False):
        self.screen, self.original = screen, observation
        self.region = _focused_region(observation.frame, allow_white=allow_white)
        # This guard may be created after the first chunk was acknowledged.
        # Only history from the original observation can establish its field;
        # newly typed text or a later connection must never supply that proof.
        frames = tuple(frame for frame in getattr(screen, "recent_frames", ())
                       if frame.generation == observation.generation
                       and 0 <= observation.updated - frame.updated <= 4)
        if self.region is None:
            self.region = temporal_focused_region(observation.frame, frames, allow_white=allow_white)
        if self.region is None and allow_white:
            # Spotlight may be observed during the caret's dark phase. Only a
            # proven blink on otherwise identical pixels can recover its field.
            for recent in reversed(frames[-12:]):
                candidate = _focused_region(recent.frame, allow_white=True)
                if candidate is not None and not screen_compare.observation_changed(
                        recent.frame, observation.frame, caret_frames=frames):
                    self.region = candidate
                    break

    def __call__(self, *, stage="context"):
        current = require_fresh(self.screen)
        region = self.region if stage in {"accept", "chunk"} else None
        if (current.generation != self.original.generation
                or screen_compare.observation_changed(
                    self.original.frame, current.frame, input_region=region,
                    caret_frames=getattr(self.screen, "recent_frames", ()))):
            raise RuntimeError("screen changed since the original observation; observe again before acting")
        return current
