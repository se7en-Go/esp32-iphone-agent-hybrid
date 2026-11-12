import asyncio
import base64
import time
from types import SimpleNamespace

import pytest
from test_controls import FakePhone, Screen, jpeg

from hidagent import feedback, ocr
from hidagent.app import ControlAction
from hidagent.control import Controls
from hidagent.freshness import snapshot


@pytest.fixture(autouse=True)
def clean_ocr():
    ocr.set_backend(None)
    yield
    ocr.set_backend(None)


def test_text_resolution_guard_and_diff_share_ocr_and_exact_result_frame(monkeypatch):
    phone, screen = FakePhone(), Screen()
    before, after, advanced = screen.frame, jpeg('black'), jpeg('red')
    reads, waits = [], []

    def read(frame):
        reads.append(frame)
        return [ocr.TextBox('设置' if frame == before else '主页', (.2, .3, .8, .7), (.5, .5), .99)]

    ocr.set_backend(SimpleNamespace(read_text=read))

    async def tap(x, y, *, before_click, target_box, target_frame):
        assert target_frame == before and target_box == (.2, .3, .8, .7)
        before_click()
        screen.frame, screen.updated = after, time.time()
        accepted = snapshot(screen)
        screen.frame = advanced  # video advances after A's final evidence check
        return {'clicked': True, 'screen_changed': True, 'fresh': True,
                'effect_verified': False, 'completed_at': time.time(),
                '_after_frame': after, '_after_snapshot': accepted}

    async def forbidden_wait(*args):
        waits.append(args)
        raise AssertionError('A already waited for the result frame')

    phone.tap_verified = tap
    monkeypatch.setattr(feedback, 'wait_after_action', forbidden_wait)
    controls = Controls(phone, screen)
    result = asyncio.run(controls.perform(ControlAction(
        action='tap_text', text='设置', screen_id=controls.observation()['screen_id'], ocr_diff=True)))
    assert reads == [before, after]
    assert not waits
    assert base64.b64decode(result['image_base64']) == after
    assert result['screen_diff']['texts_appeared'] == ['主页']
    assert result['effect_verified'] is False
    assert not any(key.startswith('_') for key in result)


def test_default_coordinate_tap_never_starts_ocr(monkeypatch):
    phone, screen = FakePhone(), Screen()
    async def tap(*_, **kwargs):
        accepted = snapshot(screen)
        return {'clicked': True, 'screen_changed': False, 'fresh': True,
                '_after_frame': screen.frame, '_after_snapshot': accepted}
    phone.tap_verified = tap
    ocr.set_backend(SimpleNamespace(read_text=lambda _: pytest.fail('default tap ran OCR')))
    controls = Controls(phone, screen)
    result = asyncio.run(controls.perform(ControlAction(
        action='tap', x=.5, y=.5, screen_id=controls.observation()['screen_id'])))
    assert result['screen_diff']['text_status'] == 'disabled'


def test_replay_is_rejected_before_observation_and_ocr():
    screen = Screen()
    screen.frame_ts = 100
    phone = SimpleNamespace(_clock=10000 - screen.updated * 1000, _clock_link=1,
                            device=SimpleNamespace(links=1))
    screen._freshness_phone = phone
    with pytest.raises(ValueError, match='重放'):
        Controls(phone, screen).observation()


def test_settling_cannot_replace_post_action_evidence_with_pre_action_frame(monkeypatch):
    from hidagent.freshness import fresh_frame

    screen = Screen()
    screen.frame_ts = 10_000
    screen._freshness_phone = SimpleNamespace(
        _clock=10_000-time.time()*1000, _clock_link=1, device=SimpleNamespace(links=1))
    completed_at = time.time()-.5

    async def fresh(*_):
        screen.updated = time.time()
        screen.frame_ts = int(screen.updated*1000+screen._freshness_phone._clock)
        return screen.frame

    async def settled(**_):
        screen.frame, screen.updated, screen.frame_ts = jpeg('black'), time.time(), 9_300

    screen.fresh, screen.settled = fresh, settled
    monkeypatch.setattr(feedback, 'MIRROR_LAG_SECONDS', 0)
    controls = Controls(FakePhone(), screen)
    started = time.perf_counter()
    result = asyncio.run(controls._respond(
        jpeg(), completed_at, started, started, True, None, {}, ocr_diff=False))
    assert fresh_frame(screen, since=completed_at) is None
    assert result['fresh'] is False
    assert result['changed'] is None
    assert result['screen_diff']['status'] == 'stale'


def test_disconnect_after_tap_evidence_keeps_changed_unknown():
    phone, screen = FakePhone(), Screen()
    accepted = snapshot(screen)

    async def tap(*_, **kwargs):
        screen.live = False
        return {'clicked': True, 'screen_changed': True, 'fresh': True,
                '_after_frame': accepted.frame, '_after_snapshot': accepted}

    phone.tap_verified = tap
    controls = Controls(phone, screen)
    token = controls.observation()['screen_id']
    result = asyncio.run(controls.perform(ControlAction(action='tap', x=.5, y=.5, screen_id=token)))
    assert result['fresh'] is False
    assert result['changed'] is None
    assert result['effect_verified'] is False
