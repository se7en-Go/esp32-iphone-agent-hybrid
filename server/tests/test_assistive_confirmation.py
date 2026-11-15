"""A silent early Home frame needs new causal video, never a second click."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fake_clock import ManualClock
from test_assistive_home import Mirror, frame, make_phone

from hidagent import assistive_vision, freshness


def rig(tmp_path, monkeypatch, *, recovery="fresh", interrupt=None, early_home=True):
    clock = ManualClock()
    clock.install(monkeypatch)

    class SilentMirror(Mirror):
        generation = 1

        def publish_recovery(self):
            self.show(frame(home=recovery != "changed"))
            if recovery == "stale":
                self.updated = time.time() - 6
            elif recovery == "old_receipt":
                self.updated = self.early_received_at

        async def send_guarded(self, command, before_write, **kwargs):
            if command == "move 2 0":
                self.wake_checked = True
                if interrupt == "stop":
                    self.abort_epoch += 1
                elif interrupt == "generation":
                    self.generation += 1
                elif interrupt == "link":
                    self.links += 1
                elif interrupt == "changed":
                    self.show(frame())
                elif interrupt == "disconnected":
                    self.live = False
            before_write()
            await self.send(command)

        async def send(self, command):
            self.sent.append(command)
            if command == "click":
                self.clicks.append(self.pos)

                def early_effect():
                    self.show(frame(home=early_home))
                    self.early_received_at = self.updated

                clock.call_later(.3, early_effect)
            elif command == "move -2 0" and recovery != "silent":
                clock.call_later(1.8, self.publish_recovery)
            await asyncio.sleep(.02)

    mirror = SilentMirror()
    mirror.clock = clock
    mirror.show(frame(menu=True))
    phone = make_phone(tmp_path, mirror)
    phone._pos, phone._link = (0, 0), 0
    return phone, mirror, clock


def test_silent_early_home_gets_one_bounded_source_refresh(tmp_path, monkeypatch):
    phone, mirror, clock = rig(tmp_path, monkeypatch)
    asyncio.run(phone.go_home())
    assert mirror.sent[1:] == ["click", "move 2 0", "move -2 0"]
    assert len(mirror.clicks) == 1
    assert phone.assistive_touch._saved["verified_route"] == "assistive-touch"
    observation = phone.assistive_touch._last_wait_observation
    assert observation.updated > phone.last_tap["completed_at"] + freshness.MIRROR_LAG_SECONDS
    assert observation.updated > mirror.early_received_at
    assert assistive_vision.is_home_screen(observation.frame)
    assert clock.elapsed < 11


@pytest.mark.parametrize("recovery", ["silent", "stale", "old_receipt", "changed"])
def test_home_refresh_needs_new_eligible_home_frame(tmp_path, monkeypatch, recovery):
    phone, mirror, clock = rig(tmp_path, monkeypatch, recovery=recovery)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.go_home())
    assert mirror.sent[1:] == ["click", "move 2 0", "move -2 0"]
    assert len(mirror.clicks) == 1
    assert "verified_route" not in phone.assistive_touch._saved
    assert clock.elapsed < 11


@pytest.mark.parametrize("interrupt", ["stop", "generation", "link", "changed", "disconnected"])
def test_home_refresh_rechecks_original_session_and_candidate_before_write(tmp_path, monkeypatch, interrupt):
    phone, mirror, _ = rig(tmp_path, monkeypatch, interrupt=interrupt)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.go_home())
    assert mirror.wake_checked
    assert mirror.sent[1:] == ["click"]
    assert len(mirror.clicks) == 1
    assert "verified_route" not in phone.assistive_touch._saved


def test_failed_home_click_without_visible_home_does_not_refresh(tmp_path, monkeypatch):
    phone, mirror, _ = rig(tmp_path, monkeypatch, early_home=False)
    with pytest.raises(RuntimeError):
        asyncio.run(phone.go_home())
    assert mirror.sent[1:] == ["click"]
    assert "verified_route" not in phone.assistive_touch._saved


def test_recovery_cannot_confirm_home_after_device_replaced_during_final_wait(tmp_path, monkeypatch):
    phone, mirror, _ = rig(tmp_path, monkeypatch)
    original_ensure, original_fresh = phone.ensure_observation, mirror.fresh
    armed = replaced = False

    async def ensure(**kwargs):
        nonlocal armed
        result = await original_ensure(**kwargs)
        armed = kwargs.get("before_refresh") is not None
        return result

    async def fresh(*args, **kwargs):
        nonlocal replaced
        if armed and not replaced:
            phone.device = SimpleNamespace(links=mirror.links, abort_epoch=mirror.abort_epoch)
            replaced = True
        return await original_fresh(*args, **kwargs)

    monkeypatch.setattr(phone, "ensure_observation", ensure)
    monkeypatch.setattr(mirror, "fresh", fresh)
    with pytest.raises(RuntimeError, match="设备连接已变化"):
        asyncio.run(phone.go_home())
    assert replaced
    assert mirror.sent[1:] == ["click", "move 2 0", "move -2 0"]
    assert "verified_route" not in phone.assistive_touch._saved
