import asyncio
import io
import json
import time

import pytest
from fake_clock import ManualClock
from home_scene import draw_home
from PIL import Image, ImageDraw

from hidagent import assistive, assistive_vision
from hidagent.device import DeviceError
from hidagent.phone import Phone
from hidagent.screen import Screen


def frame(*, menu=False, home=False, button=(333, 598), menu_offset=0, panel_color="#252525"):
    image = Image.new("RGB", (360, 780), "#a9bdcf" if home else "#e5dbc5")
    draw = ImageDraw.Draw(image)
    if home:
        draw_home(image)
    if menu:
        draw.rounded_rectangle((50, 245+menu_offset, 310, 505+menu_offset), radius=28, fill=panel_color)
        for x, y in ((180, 288), (100, 332), (260, 332), (100, 426), (260, 426), (180, 457)):
            y += menu_offset
            draw.ellipse((x - 14, y - 14, x + 14, y + 14), outline="white", width=3)
    elif button is not None:
        x, y = button
        draw.rounded_rectangle((x-23, y-23, x+23, y+23), radius=11, fill="#333333")
        draw.ellipse((x-15, y-15, x+15, y+15), outline="#eeeeee", width=4)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class Mirror:
    """Clicks change the mirror; HID Home paths deliberately do nothing."""

    live = True
    links = 0
    abort_epoch = 0

    def __init__(self, delay=.001):
        self.frame, self.updated = frame(), time.time()
        self.sent = []
        self.clicks = []
        self.pos = (0, 0)
        self.delay = delay
        self.home_target = (180, 457)
        self.button_target = (333, 598)
        self.return_home = True
        self.menu_opens = True

    def show(self, image):
        self.frame, self.updated = image, time.time()

    async def send(self, command):
        self.sent.append(command)
        if command == "home":
            self.pos = (0, 0)
        elif command.startswith("move "):
            dx, dy = map(int, command.split()[1:])
            self.pos = self.pos[0] + dx, self.pos[1] + dy
        elif command == "click":
            self.clicks.append(self.pos)
            target = self.button_target if len(self.clicks) == 1 else self.home_target
            if max(abs(a - b) for a, b in zip(target, self.pos, strict=True)) > 12:
                return
            if len(self.clicks) == 1 and not self.menu_opens:
                return
            image = frame(menu=True) if len(self.clicks) == 1 else frame(home=self.return_home)
            self.clock.call_later(self.delay, self.show, image)

    async def fresh(self, since, timeout=0.8):
        if self.updated > since:
            return self.frame
        await asyncio.sleep(min(timeout, 0.01))
        return None

    async def settled(self, **kwargs):
        pass


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    # Model frame delivery and firmware waits explicitly. Host clock resolution,
    # timer scheduling and JPEG processing time must not change the evidence.
    clock = ManualClock()
    clock.install(monkeypatch)
    monkeypatch.setattr(Mirror, "clock", clock, raising=False)
    monkeypatch.setattr("hidagent.freshness.MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr("hidagent.phone.TAP_GAP", 0)
    monkeypatch.setattr("hidagent.feedback.MIRROR_LAG_SECONDS", 0)
    monkeypatch.setattr("hidagent.phone.TAP_EFFECT_WAIT", .02)
    monkeypatch.setattr(assistive, "MENU_WAIT", .4)
    monkeypatch.setattr(assistive, "RESULT_WAIT", .4)
    monkeypatch.setattr(assistive, "STABLE_QUIET", .04)
    return clock


def make_phone(tmp_path, mirror, **kwargs):
    phone = Phone(mirror, tmp_path / "calibration.json", mirror, **kwargs)
    phone.span = {"w": 360, "h": 780}
    for samples in phone._seen.values():
        samples.extend([(0., 0.), (.5, .5), (.99, .99)])
    async def measured_pointer(near, **kwargs):
        # These fixtures exercise navigation routes/effects; scoped navigation
        # now requires measured localization, covered separately by pixel tests.
        return near
    phone._find_pointer = measured_pointer
    return phone


def test_go_home_opens_assistive_touch_then_taps_home(tmp_path, monkeypatch):
    mirror = Mirror()
    phone = make_phone(tmp_path, mirror)

    async def measured_pointer(near, **kwargs):
        return near

    monkeypatch.setattr(phone, "_find_pointer", measured_pointer)

    asyncio.run(phone.go_home())

    assert mirror.sent.count("click") == 2
    assert not {"press", "homescreen", "key cmd+h"}.intersection(mirror.sent)
    assert mirror.frame == frame(home=True)
    assert mirror.clicks[0] == pytest.approx((333, 598), abs=3)
    assert mirror.clicks[1] == pytest.approx((180, 458), abs=3)


@pytest.mark.parametrize("target", ["menu", "bubble"])
@pytest.mark.parametrize("transition", ["after_selection", "inside_tap"])
def test_home_target_never_reanchors_on_reconnected_identical_screen(tmp_path, monkeypatch, target, transition):
    from types import SimpleNamespace

    async def scenario():
        screen = Screen()
        screen._on_ready()
        screen._on_jpeg(frame(menu=target == "menu"))
        selected = screen.frame
        commands = []

        async def send(command):
            commands.append(command)

        phone = Phone(SimpleNamespace(links=1, abort_epoch=0, send=send), tmp_path / "cal.json", screen)
        home = phone.assistive_touch
        home._generation = screen.generation

        def reconnect():
            screen._on_ready()
            screen._on_jpeg(selected)

        async def measured_pointer(x, y, **kwargs):
            phone._pointer_confirmed = True
            return x, y

        async def unchanged_effect(*_args):
            return False, True

        phone.point_verified = measured_pointer
        phone._tap_effect = unchanged_effect
        if transition == "inside_tap":
            original = phone.ensure_observation

            async def reanchor():
                reconnect()
                return await original()

            phone.ensure_observation = reanchor
        elif target == "menu":
            reconnect()
        else:
            original = assistive_vision.find_button_target

            def select_then_reconnect(*args, **kwargs):
                detected = original(*args, **kwargs)
                reconnect()
                return detected

            monkeypatch.setattr(assistive_vision, "find_button_target", select_then_reconnect)
        with pytest.raises(RuntimeError, match="画面连接已变化"):
            if target == "menu":
                await home._menu_home(0, selected, assistive_vision.find_panel(selected))
            else:
                await home._assistive_home(0)
        assert "click" not in commands and "press" not in commands

    asyncio.run(scenario())


@pytest.mark.parametrize("menu", [False, True])
@pytest.mark.parametrize("transition", ["link", "abort"])
def test_home_keeps_original_device_session_inside_tap_observation(tmp_path, menu, transition):
    from types import SimpleNamespace

    from hidagent.freshness import require_fresh
    from hidagent.navigation_context import for_phone

    async def scenario():
        screen = Screen()
        screen._on_ready()
        screen._on_jpeg(frame(menu=menu))
        commands = []

        async def send(command):
            commands.append(command)
            if command == "click":
                raise RuntimeError("unexpected click across the original Home session")

        device = SimpleNamespace(links=1, abort_epoch=0, send=send)
        phone = Phone(device, tmp_path / "cal.json", screen)
        context = for_phone(phone, "home", require_fresh(screen))
        original = phone.ensure_observation
        observations = 0

        async def reconnect_inside_tap():
            nonlocal observations
            observations += 1
            if observations == 2:
                if transition == "link":
                    device.links += 1
                else:
                    device.abort_epoch += 1
            return await original()

        async def measured(x, y, **kwargs):
            phone._pointer_confirmed = True
            return x, y

        async def noop(*args, **kwargs):
            return None

        phone.ensure_observation = reconnect_inside_tap
        phone.point_verified = measured
        phone._catch_up = noop
        with pytest.raises(RuntimeError):
            await phone.go_home(navigation_context=context)
        assert observations >= 2
        assert not commands

    asyncio.run(scenario())


@pytest.mark.parametrize("menu", [False, True])
def test_home_pins_mirror_generation_before_first_observation_await(tmp_path, menu):
    from types import SimpleNamespace

    async def scenario():
        screen = Screen()
        screen._on_ready()
        screen._on_jpeg(frame(menu=menu))
        selected = screen.frame
        commands = []

        async def send(command):
            commands.append(command)

        phone = Phone(SimpleNamespace(links=1, abort_epoch=0, send=send), tmp_path / "cal.json", screen)
        original = phone.ensure_observation
        observations = 0

        async def reconnect_during_initial_observation():
            nonlocal observations
            observations += 1
            if observations == 1:
                screen._on_ready()
                screen._on_jpeg(selected)
            return await original()

        async def measured(x, y, **kwargs):
            phone._pointer_confirmed = True
            return x, y

        async def unchanged_effect(*args, **kwargs):
            return False, True

        async def noop(*args, **kwargs):
            return None

        phone.ensure_observation = reconnect_during_initial_observation
        phone.point_verified = measured
        phone._tap_effect = unchanged_effect
        phone._catch_up = noop
        phone.assistive_touch._wait = noop
        with pytest.raises(RuntimeError, match="画面连接已变化"):
            await phone.go_home()
        assert not commands
        assert observations == 1

    asyncio.run(scenario())


def test_timestamped_screen_uses_wall_clock_offset_during_home(tmp_path):
    """Home must retain the wall-domain bound established by a real board status."""
    mirror = Mirror(delay=.01)
    screen = Screen()
    board_start, host_start = 123_456, time.monotonic()

    def publish(image):
        screen._on_ts(board_start + round((time.monotonic() - host_start) * 1000))
        screen._on_jpeg(image)

    async def query(command):
        assert command == "status"
        return json.dumps({"up": board_start + round((time.monotonic() - host_start) * 1000)})

    mirror.show, mirror.query = publish, query
    publish(frame())
    phone = Phone(mirror, tmp_path / "calibration.json", screen)
    phone.span = {"w": 360, "h": 780}
    for samples in phone._seen.values():
        samples.extend([(0., 0.), (.5, .5), (.99, .99)])
    async def measured_pointer(near, **kwargs):
        return near
    phone._find_pointer = measured_pointer

    async def run():
        await phone.ensure_observation()
        assert phone._clock is not None and phone._mono_clock is not None
        assert phone._clock != phone._mono_clock
        await phone.go_home()

    asyncio.run(run())
    assert len(mirror.clicks) == 2
    assert screen.frame == frame(home=True)


@pytest.mark.parametrize("wall_tick", [.001, 1/64])
def test_home_with_scheduled_frames_and_coarse_wall_clock(tmp_path, monkeypatch, short_waits, wall_tick):
    clock = short_waits
    monkeypatch.setattr(time, "time", lambda: clock.wall_origin + (clock.elapsed // wall_tick) * wall_tick)
    # Each new image is delivered on a later clock tick; reading time alone
    # cannot fabricate a later receipt or satisfy the menu's quiet interval.
    mirror = Mirror(delay=wall_tick)
    asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert len(mirror.clicks) == 2
    assert mirror.frame == frame(home=True)
    assert not {"homescreen", "key cmd+h"}.intersection(mirror.sent)


def test_delayed_menu_and_home_then_silent_mirror(tmp_path):
    mirror = Mirror(delay=.06)
    asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert len(mirror.clicks) == 2 and mirror.frame == frame(home=True)


def test_position_survives_restart_as_a_search_hint(tmp_path, monkeypatch):
    mirror = Mirror()
    phone = make_phone(tmp_path, mirror)
    asyncio.run(phone.go_home())
    data = json.loads((tmp_path / "calibration.assistive-touch.json").read_text())
    assert data["last_position"] == pytest.approx((333/360, 598/780), abs=.01)
    # Measured taps may update calibration; the AssistiveTouch hint belongs only
    # to its own profile, never to the pointer calibration file.
    assert "last_position" not in json.loads((tmp_path / "calibration.json").read_text())
    detect = assistive_vision.find_button_target
    hints = []

    def find_button(image, near=None):
        hints.append(near)
        return detect(image, near=near)

    monkeypatch.setattr(assistive_vision, "find_button_target", find_button)
    second = Mirror()
    asyncio.run(make_phone(tmp_path, second).go_home())
    assert len(second.clicks) == 2
    assert hints == [tuple(data["last_position"])]


@pytest.mark.parametrize("source", ["memory", "file", "configured"])
def test_removed_button_never_uses_saved_or_configured_coordinate(tmp_path, monkeypatch, source):
    position = (333/360, 598/780)
    if source == "file":
        (tmp_path / "calibration.assistive-touch.json").write_text(
            json.dumps({"last_position": position, "aspect": 360/780}))
    mirror = Mirror()
    mirror.show(frame(button=None))
    options = {"assistive_touch_position": position} if source == "configured" else {}
    phone = make_phone(tmp_path, mirror, **options)
    if source == "memory":
        phone.assistive_touch.last_position = position
        phone.assistive_touch.last_aspect = 360/780
    monkeypatch.setattr(assistive, "MENU_WAIT", .02)
    assert not asyncio.run(phone.assistive_touch._assistive_home(0))
    assert not mirror.clicks


def test_moved_button_uses_current_detection_instead_of_cached_coordinate(tmp_path):
    (tmp_path / "calibration.assistive-touch.json").write_text(
        json.dumps({"last_position": [.925, .767], "aspect": 360/780}))
    mirror = Mirror()
    mirror.button_target = (27, 320)
    mirror.show(frame(button=mirror.button_target))
    asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert mirror.clicks[0] == pytest.approx(mirror.button_target, abs=3)
    assert len(mirror.clicks) == 2


@pytest.mark.parametrize("source", ["constructor", "environment", "file"])
def test_configured_position_takes_precedence(tmp_path, monkeypatch, source):
    position = (333/360, 598/780)
    options = {}
    if source == "constructor":
        options["assistive_touch_position"] = position
        monkeypatch.setenv("HIDAGENT_ASSISTIVE_TOUCH_POSITION", "0.1,0.2")
    elif source == "environment":
        monkeypatch.setenv("HIDAGENT_ASSISTIVE_TOUCH_POSITION", ",".join(map(str, position)))
    else:
        (tmp_path / "calibration.assistive-touch.json").write_text(json.dumps({"position": position}))
    detect = assistive_vision.find_button_target
    hints = []

    def find_button(image, near=None):
        hints.append(near)
        return detect(image, near=near)

    monkeypatch.setattr(assistive_vision, "find_button_target", find_button)
    mirror = Mirror()
    asyncio.run(make_phone(tmp_path, mirror, **options).go_home())
    assert len(mirror.clicks) == 2
    assert hints == [position]
    if source == "file":
        assert json.loads((tmp_path / "calibration.assistive-touch.json").read_text())["position"] == list(position)


@pytest.mark.parametrize("position", [(float("nan"), .5), (.5, float("inf")), (-.1, .5), (1.1, .5), [.5]])
def test_invalid_configured_position_is_rejected(tmp_path, position):
    with pytest.raises(ValueError):
        make_phone(tmp_path, Mirror(), assistive_touch_position=position)


def test_corrupt_position_cache_does_not_break_phone(tmp_path):
    (tmp_path / "calibration.assistive-touch.json").write_text("{broken")
    mirror = Mirror()
    asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert len(mirror.clicks) == 2


@pytest.mark.parametrize("language", ["主屏幕", "Home"])
def test_find_text_hook_overrides_geometry(tmp_path, language):
    calls = []

    def find_text(image, text):
        assert image == frame(menu=True)
        calls.append(text)
        return (260/360, 332/780) if text == language else None

    mirror = Mirror()
    mirror.home_target = (260, 332)
    phone = make_phone(tmp_path, mirror, find_text=find_text)
    asyncio.run(phone.go_home())
    assert mirror.clicks[1] == mirror.home_target
    assert calls == (["主屏幕"] if language == "主屏幕" else ["主屏幕", "Home"])


@pytest.mark.parametrize("result", [None, (.05, .05), (float("nan"), .5), "error"])
def test_unusable_ocr_uses_layout(tmp_path, result):
    def find_text(image, text):
        if result == "error":
            raise RuntimeError("OCR unavailable")
        return result

    mirror = Mirror()
    asyncio.run(make_phone(tmp_path, mirror, find_text=find_text).go_home())
    assert mirror.clicks[1] == pytest.approx(mirror.home_target, abs=3)


def test_ocr_exception_contents_never_appear_in_logs(tmp_path, caplog):
    def find_text(image, text):
        raise RuntimeError("sensitive-screen-marker-password-123")

    mirror = Mirror()
    asyncio.run(make_phone(tmp_path, mirror, find_text=find_text).go_home())
    assert len(mirror.clicks) == 2
    assert "文字定位失败" in caplog.text
    assert "sensitive-screen-marker" not in caplog.text


@pytest.mark.parametrize("changing", ["bounds", "content"])
def test_moving_menu_timeout_never_authorizes_home_click(tmp_path, monkeypatch, changing):
    mirror = Mirror()
    mirror.settled = Screen.settled.__get__(mirror)
    monkeypatch.setattr(assistive, "MENU_WAIT", 1.1)
    monkeypatch.setattr(assistive, "RESULT_WAIT", .02)
    monkeypatch.setattr(assistive, "STABLE_QUIET", .2)
    phone = make_phone(tmp_path, mirror)
    original_send = mirror.send
    changes = []

    def animate():
        step = len(changes) % 2
        mirror.show(frame(menu=True, menu_offset=step*18 if changing == "bounds" else 0,
                          panel_color="#404040" if step and changing == "content" else "#252525"))
        changes.append(time.monotonic())
        mirror.clock.call_later(.07, animate)

    async def send(command):
        await original_send(command)
        if command == "click" and len(mirror.clicks) == 1:
            mirror.clock.call_later(.07, animate)

    mirror.send = send

    async def run():
        assert not await phone.assistive_touch._assistive_home(0)

    asyncio.run(run())
    assert len(changes) >= 5
    assert len(mirror.clicks) == 1


def test_menu_arriving_near_deadline_does_not_bypass_quiet_interval(tmp_path, monkeypatch):
    mirror = Mirror(delay=.22)
    monkeypatch.setattr(assistive, "MENU_WAIT", .35)
    monkeypatch.setattr(assistive, "STABLE_QUIET", .2)
    assert not asyncio.run(make_phone(tmp_path, mirror).assistive_touch._assistive_home(0))
    assert len(mirror.clicks) == 1


def test_stable_menu_with_repeated_identical_frames_can_succeed(tmp_path, monkeypatch):
    mirror = Mirror()
    original_fresh = mirror.fresh

    async def fresh(since, timeout):
        result = await original_fresh(since, timeout)
        if result is None:
            mirror.show(mirror.frame)
            return mirror.frame
        return result

    mirror.fresh = fresh
    monkeypatch.setattr(assistive, "STABLE_QUIET", .2)
    monkeypatch.setattr(assistive, "MENU_WAIT", .8)
    monkeypatch.setattr(assistive, "RESULT_WAIT", .8)
    asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert len(mirror.clicks) == 2
    assert mirror.frame == frame(home=True)


def test_frame_arriving_as_fresh_times_out_invalidates_previous_candidate(tmp_path):
    mirror = Mirror()
    mirror.show(frame(menu=True))
    original_fresh = mirror.fresh

    async def fresh(since, timeout):
        if mirror.updated > since:
            return await original_fresh(since, timeout)
        await asyncio.sleep(.05)
        mirror.show(frame())
        return None

    mirror.fresh = fresh
    phone = make_phone(tmp_path, mirror)
    result = asyncio.run(phone.assistive_touch._wait(
        0, mirror.updated-1, .3, assistive_vision.find_panel, stable=True))
    assert result is None


@pytest.mark.parametrize("rounded", [False, True])
def test_unrelated_dialog_after_button_tap_does_not_authorize_home(tmp_path, monkeypatch, rounded):
    mirror = Mirror()
    mirror.menu_opens = False
    image = Image.open(io.BytesIO(frame())).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((50, 245, 310, 505), radius=28 if rounded else 0, fill="#252525")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    original_send = mirror.send

    async def send(command):
        await original_send(command)
        if command == "click":
            mirror.show(buffer.getvalue())

    def find_text(image, text):
        raise AssertionError("An unrelated dialog must not reach Home targeting")

    mirror.send = send
    monkeypatch.setattr(assistive, "MENU_WAIT", .25)
    phone = make_phone(tmp_path, mirror, find_text=find_text)
    assert not asyncio.run(phone.assistive_touch._assistive_home(0))
    assert len(mirror.clicks) == 1


def test_custom_home_slot_is_relative_to_detected_panel(tmp_path):
    mirror = Mirror()
    mirror.home_target = (100, 332)
    phone = make_phone(tmp_path, mirror, assistive_touch_layout={"home": (.19, .33)})
    asyncio.run(phone.go_home())
    assert mirror.clicks[1] == pytest.approx(mirror.home_target, abs=3)


@pytest.mark.parametrize("menu_opens", [False, True])
def test_missing_menu_or_menu_dismissal_is_not_home(tmp_path, monkeypatch, menu_opens):
    mirror = Mirror()
    mirror.menu_opens, mirror.return_home = menu_opens, False
    monkeypatch.setattr(assistive, "MENU_WAIT", .4)
    monkeypatch.setattr(assistive, "RESULT_WAIT", .05)
    with pytest.raises(RuntimeError, match="未确认主屏幕"):
        asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert len(mirror.clicks) == (2 if menu_opens else 1)
    assert mirror.sent[-2:] == ["homescreen", "key cmd+h"]


def test_abort_while_waiting_for_menu_sends_no_second_tap_or_fallback(tmp_path):
    mirror = Mirror()

    async def stopped(since, timeout):
        mirror.abort_epoch += 1
        return None

    mirror.fresh = stopped
    with pytest.raises(DeviceError, match="紧急停止"):
        asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert len(mirror.clicks) == 1
    assert not {"press", "homescreen", "key cmd+h"}.intersection(mirror.sent)


def test_fallback_dismissing_stuck_menu_is_not_success(tmp_path, monkeypatch):
    mirror = Mirror()
    mirror.home_target = (20, 20)  # The Home tap misses, leaving the menu open.
    original_send = mirror.send

    async def send(command):
        await original_send(command)
        if command == "release":
            mirror.show(frame())  # Swipe only closes the panel.
        elif command == "homescreen":
            mirror.clock.call_later(.001, mirror.show, frame(home=True))

    mirror.send = send
    monkeypatch.setattr(assistive, "RESULT_WAIT", .4)
    asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert mirror.sent[-1] == "homescreen"
    assert mirror.frame == frame(home=True)


def test_transient_frame_does_not_verify_home_before_settling(tmp_path, monkeypatch):
    mirror = Mirror()
    original_send = mirror.send

    async def send(command):
        await original_send(command)
        if command == "click" and len(mirror.clicks) == 2:
            mirror.clock.call_later(.02, mirror.show, frame(menu=True))
        elif command == "release":
            mirror.show(frame())
        elif command == "homescreen":
            mirror.clock.call_later(.001, mirror.show, frame(home=True))

    mirror.send = send
    monkeypatch.setattr(assistive, "RESULT_WAIT", .4)
    asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert mirror.sent[-1] == "homescreen"


def test_rotated_frame_does_not_use_cached_button_position(tmp_path, monkeypatch):
    (tmp_path / "calibration.assistive-touch.json").write_text(
        json.dumps({"last_position": [.91, .78], "aspect": 360/780}))
    mirror = Mirror()
    image = Image.new("RGB", (780, 360), "white")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    mirror.show(buffer.getvalue())
    monkeypatch.setattr(assistive_vision, "find_button_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(assistive, "RESULT_WAIT", .02)
    with pytest.raises(RuntimeError, match="未确认主屏幕"):
        asyncio.run(make_phone(tmp_path, mirror).go_home())
    assert not mirror.clicks


def test_changed_app_after_menu_dismissal_is_not_home(tmp_path, monkeypatch):
    mirror = Mirror()
    phone = make_phone(tmp_path, mirror)
    original_show = mirror.show
    image = Image.new("RGB", (360, 780), "#43769a")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")

    def show(image):
        original_show(buffer.getvalue() if image == frame(home=True) else image)

    mirror.show = show
    monkeypatch.setattr(assistive, "RESULT_WAIT", .6)
    assert not asyncio.run(phone.assistive_touch._assistive_home(0))
    assert len(mirror.clicks) == 2


@pytest.mark.parametrize("stale", ["arrival", "replay"])
def test_assistive_rejects_stale_frame_before_any_click(tmp_path, stale):
    mirror = Mirror()
    phone = make_phone(tmp_path, mirror)
    if stale == "arrival":
        mirror.updated -= 10
    else:
        mirror.frame_ts = 1_000
        phone.assistive_touch._clock = 11_000 - mirror.updated*1000
    assert not asyncio.run(phone.assistive_touch._assistive_home(0))
    assert not mirror.clicks


def test_result_within_mirror_lag_is_not_home_evidence(tmp_path, monkeypatch):
    mirror = Mirror()
    mirror.show(frame(home=True))
    phone = make_phone(tmp_path, mirror)
    monkeypatch.setattr("hidagent.freshness.MIRROR_LAG_SECONDS", .1)
    result = asyncio.run(phone.assistive_touch._wait(
        0, mirror.updated-.01, .2, assistive_vision.is_home_screen, stable=True))
    assert result is None


def test_home_button_refuses_blind_click_without_measured_landing_error(tmp_path, monkeypatch):
    mirror = Mirror()
    phone = make_phone(tmp_path, mirror)
    for samples in phone._seen.values():
        samples.clear()

    async def hidden(*_, **__):
        return None

    monkeypatch.setattr(phone, "_find_pointer", hidden)
    with pytest.raises(RuntimeError, match="未发送点击"):
        asyncio.run(phone.assistive_touch._assistive_home(0))
    assert not mirror.clicks
