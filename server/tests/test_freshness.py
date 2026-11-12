import time
from types import SimpleNamespace

import pytest

from hidagent import freshness


def screen(**kwargs):
    return SimpleNamespace(frame=b'frame', live=True, updated=time.time(), frame_ts=None, **kwargs)


def test_recent_arrival_does_not_authorize_old_board_replay():
    view = screen()
    view.frame_ts = 100
    assert freshness.fresh_frame(view, clock=10000 - view.updated * 1000) is None


def test_missing_clock_or_stale_receipt_refuses():
    view = screen()
    view.updated -= 10
    assert freshness.fresh_frame(view) is None
    view.updated = time.time()
    view.frame_ts = 10
    assert freshness.fresh_frame(view) is None
    with pytest.raises(RuntimeError, match='未发送点击'):
        freshness.require_fresh(view)


def test_board_rollover_and_reconnected_clock():
    view = screen()
    view.frame_ts = 0xfffffff0
    clock = (2**32 + 100) - view.updated * 1000
    assert freshness.fresh_frame(view, clock=clock)
    assert freshness.fresh_frame(view, clock=clock, clock_link=1, link=2) is None


def test_post_action_needs_eligible_frame_beyond_mirror_delay():
    view = screen()
    assert freshness.fresh_frame(view, since=view.updated - 1) is None
    assert freshness.fresh_frame(view, since=view.updated - 2)


def test_screen_snapshot_keeps_frame_timestamp_pair_and_invalidates_on_reconnect():
    from hidagent.screen import Screen

    view = Screen()
    view._on_ready()
    view._on_ts(123)
    view._on_jpeg(b'first')
    first = view.snapshot()
    view._on_ts(456)  # pending timestamp must not relabel the published frame
    assert view.snapshot() == first
    view._on_jpeg(b'second')
    assert (view.snapshot().frame, view.snapshot().frame_ts) == (b'second', 456)
    view._on_ready()
    assert view.snapshot().generation == first.generation + 1
    assert view.snapshot().frame is None
    assert freshness.fresh_frame(view) is None
