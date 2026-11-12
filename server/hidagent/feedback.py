"""Wait for action evidence beyond the mirror's delivery delay."""

from .freshness import MIRROR_LAG_SECONDS, fresh_frame


async def wait_after_action(screen, completed_at: float, timeout: float = 1.5) -> bytes | None:
    """Exclude queued pre-action frames; no qualifying arrival means unknown, even on a still screen."""
    arrived = await screen.fresh(completed_at + MIRROR_LAG_SECONDS, timeout + MIRROR_LAG_SECONDS)
    if arrived is None:
        return None
    frame = fresh_frame(screen, since=completed_at)
    return frame.frame if frame else None
