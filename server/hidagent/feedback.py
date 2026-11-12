"""Wait for action evidence beyond the mirror's delivery delay."""

# Board frame_ts is receipt time, not iPhone capture time. Budget the observed AirPlay delay.
MIRROR_LAG_SECONDS = 1.7


async def wait_after_action(screen, completed_at: float, timeout: float = 1.5) -> bytes | None:
    """Exclude queued pre-action frames; no qualifying arrival means unknown, even on a still screen."""
    return await screen.fresh(completed_at + MIRROR_LAG_SECONDS, timeout + MIRROR_LAG_SECONDS)
