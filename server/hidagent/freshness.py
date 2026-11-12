"""One eligibility policy for observations, pointer checks and action evidence.

Board time measures receipt at the board, not iPhone capture. A live transport or
recent host arrival alone cannot establish eligibility for timestamped video.
"""

import math
import time
from dataclasses import dataclass

MIRROR_LAG_SECONDS = 1.7
MAX_FRAME_AGE = 5.0
MAX_REPLAY_LAG = 2.0


@dataclass(frozen=True)
class FrameSnapshot:
    frame: bytes | None
    updated: float
    frame_ts: int | None
    generation: int
    live: bool


def snapshot(screen) -> FrameSnapshot:
    if screen is None:
        return FrameSnapshot(None, 0, None, 0, False)
    take = getattr(screen, 'snapshot', None)
    if isinstance(take, FrameSnapshot):
        return take
    if callable(take):
        observed = take()
        if isinstance(observed, FrameSnapshot):
            # Pixels stay frozen, but a stopped or disconnected screen is no longer eligible.
            if observed.live and hasattr(screen, "live") and not screen.live:
                return FrameSnapshot(observed.frame, observed.updated, observed.frame_ts,
                                     observed.generation, False)
            return observed
    if take is not None and (hasattr(take, 'board_ts') or hasattr(take, 'frame_ts')):
        return FrameSnapshot(
            getattr(take, 'frame', None), getattr(screen, 'updated', getattr(take, 'updated', 0)),
            getattr(take, 'board_ts', getattr(take, 'frame_ts', None)),
            getattr(take, 'generation', getattr(screen, 'generation', 0)),
            bool(getattr(take, 'observed_live', getattr(screen, 'live', False))))
    return FrameSnapshot(screen.frame, getattr(screen, 'updated', 0),
                         getattr(screen, 'frame_ts', None), getattr(screen, 'generation', 0), screen.live)


def board_delta(newer: float, older: float) -> float:
    """Signed millis delta across a single uint32 rollover."""
    return (newer - older + 2**31) % 2**32 - 2**31


def frame_age(frame: FrameSnapshot, clock: float | None) -> float | None:
    if clock is None or frame.frame_ts is None:
        return None
    return board_delta(frame.updated * 1000 + clock, frame.frame_ts) / 1000


def fresh_frame(screen, *, clock=None, clock_link=None, link=None, since=None,
                max_age=MAX_FRAME_AGE, now=None) -> FrameSnapshot | None:
    frame = snapshot(screen)
    now = time.time() if now is None else now
    if (not frame.live or not frame.frame or not math.isfinite(frame.updated)
            or frame.updated <= 0 or not -.1 <= now - frame.updated <= max_age):
        return None
    phone = getattr(screen, '_freshness_phone', None)
    uncertainty = 0.0
    if phone is not None:
        if clock is None:
            clock = phone._clock
        if clock_link is None:
            clock_link = phone._clock_link
        if link is None:
            link = getattr(phone.device, 'links', 0)
        uncertainty = getattr(phone, '_clock_uncertainty', 0.0)
    if clock_link is not None and link is not None and clock_link != link:
        return None
    if frame.frame_ts is not None:
        age = frame_age(frame, clock)
        if age is None or not -uncertainty - .1 <= age <= MAX_REPLAY_LAG - uncertainty:
            return None
    if since is not None:
        if frame.updated <= since:
            return None
        if frame.frame_ts is not None and clock is not None:
            # Firmware stamps board receipt: the image may still have spent the
            # whole mirror allowance between phone capture and board receipt.
            if board_delta(frame.frame_ts, since * 1000 + clock) < (MIRROR_LAG_SECONDS + uncertainty) * 1000:
                return None
        elif frame.updated <= since + MIRROR_LAG_SECONDS:
            return None
    return frame


def require_fresh(screen, **kwargs) -> FrameSnapshot:
    frame = fresh_frame(screen, **kwargs)
    if frame is None:
        raise RuntimeError('画面不够新鲜或正在重放，未发送点击；请重新观察。')
    return frame
