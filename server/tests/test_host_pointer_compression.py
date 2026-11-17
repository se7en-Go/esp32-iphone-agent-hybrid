"""Host-grounded stationary clicks tolerate only tiny local compression noise."""

import asyncio
import io

import pytest
from fake_clock import ManualClock
from PIL import Image, ImageDraw
from test_host_observed_click import evidence, no_effect, setup

from hidagent import screen_compare
from hidagent.phone import _host_observed_pixels_match

POINTER = (290 / 500, 761 / 1080)
TARGET = (.54, .66, .62, .75)


def frame(*, moved=0, hidden=False):
    image = Image.new("RGB", (500, 1080), (238, 240, 244))
    draw = ImageDraw.Draw(image)
    draw.text((279, 756), "Home", fill=(0, 122, 255))
    if not hidden:
        draw.ellipse((278 + moved, 749, 302 + moved, 773), fill=(90, 90, 90))
    return image


def encode(image):
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def edit(image, kind):
    image = image.copy()
    draw = ImageDraw.Draw(image)
    if kind == "noise":
        for y in range(729, 794, 3):
            for x in range(258, 323, 8):
                image.putpixel((x, y), tuple(min(255, value + 2) for value in image.getpixel((x, y))))
        image.putpixel((290, 761), (95, 95, 95))
    elif kind == "moved":
        return frame(moved=1)
    elif kind == "hidden":
        return frame(hidden=True)
    elif kind == "local_text":
        draw.line((292, 751, 292, 760), fill="white", width=1)
    elif kind == "brightness":
        for y in range(729, 794):
            for x in range(258, 323):
                image.putpixel((x, y), tuple(value + 1 for value in image.getpixel((x, y))))
    elif kind == "remote_text":
        draw.text((100, 400), "A different page", fill="black")
    elif kind == "size":
        return image.crop((0, 0, 499, 1080))
    return image


@pytest.mark.parametrize("qualified", [False, True])
def test_host_click_allows_bounded_local_compression_without_claiming_cv(tmp_path, qualified):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        original = encode(frame())
        current = encode(edit(frame(), "noise"))
        assert original != current and not screen_compare.observation_changed(original, current)
        screen._on_jpeg(original)
        phone._tap_effect = no_effect
        observed = await phone.observe_host_pointer() if qualified else None
        request = evidence(phone, screen)
        request.update(pointer_xy=POINTER, target_box=TARGET)
        screen._on_jpeg(current)
        try:
            result = await phone.click_observed(**request, host_observation=observed)
            assert link.written == [b"1 click\n"]
            assert result["evidence_source"] == "host_observed"
            assert result["pointer_seen"] is False and result["effect_verified"] is False
            assert not phone._pointer_confirmed and not phone._pointer_pair_verified
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("kind", ["moved", "hidden", "local_text", "brightness", "remote_text", "size"])
def test_host_click_rejects_real_change_before_input(tmp_path, qualified, kind):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        screen._on_jpeg(encode(frame()))
        phone._tap_effect = no_effect
        observed = await phone.observe_host_pointer() if qualified else None
        request = evidence(phone, screen)
        request.update(pointer_xy=POINTER, target_box=TARGET)
        screen._on_jpeg(encode(edit(frame(), kind)))
        try:
            with pytest.raises(RuntimeError, match="变化"):
                await phone.click_observed(**request, host_observation=observed)
            assert link.written == []
        finally:
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("kind", ["moved", "hidden", "local_text", "brightness", "remote_text", "size"])
def test_noise_admission_still_rechecks_pixels_at_actual_write(tmp_path, qualified, kind):
    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        screen._on_jpeg(encode(frame()))
        phone._tap_effect = no_effect
        observed = await phone.observe_host_pointer() if qualified else None
        request = evidence(phone, screen)
        request.update(pointer_xy=POINTER, target_box=TARGET)
        screen._on_jpeg(encode(edit(frame(), "noise")))
        await device._lock.acquire()
        task = asyncio.create_task(phone.click_observed(**request, host_observation=observed))
        try:
            async def queued():
                while not device._lock._waiters:
                    if task.done():
                        await task
                    await asyncio.sleep(0)
            await asyncio.wait_for(queued(), 1)
            screen._on_jpeg(encode(edit(frame(), kind)))
            device._lock.release()
            with pytest.raises(RuntimeError, match="变化"):
                await task
            assert link.written == []
        finally:
            if device._lock.locked():
                device._lock.release()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            device.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("delta", [5, 6])
def test_native_roi_includes_both_radius_endpoints_and_scales_with_width(scale, delta):
    original = frame().resize((500 * scale, 1080 * scale))
    current = original.copy()
    radius = 32 * scale
    x, y = 290 * scale + radius, 761 * scale + radius
    pixel = current.getpixel((x, y))
    current.putpixel((x, y), tuple(value - delta for value in pixel))
    # Such a tiny difference cannot be delegated to the downscaled page check.
    assert not screen_compare.observation_changed(encode(original), encode(current))
    assert _host_observed_pixels_match(encode(original), encode(current), POINTER) is (delta == 5)


def test_local_noise_is_always_measured_against_original_not_previous_accepted_frame():
    original = frame()
    first, second = original.copy(), original.copy()
    first.putpixel((290, 761), (95, 95, 95))
    second.putpixel((290, 761), (96, 96, 96))
    assert _host_observed_pixels_match(encode(original), encode(first), POINTER)
    assert _host_observed_pixels_match(encode(first), encode(second), POINTER)
    assert not _host_observed_pixels_match(encode(original), encode(second), POINTER)


def test_passive_wait_rejects_first_real_change_even_after_valid_noise(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch)

    async def scenario():
        phone, device, screen, link = setup(tmp_path)
        phone._tap_effect = no_effect
        screen._on_jpeg(encode(frame()))
        observed = await phone.observe_host_pointer()
        request = evidence(phone, screen)
        request.update(pointer_xy=POINTER, target_box=TARGET)
        screen._on_jpeg(encode(edit(frame(), "noise")))
        clock.advance(10)
        clock.call_later(.1, screen._on_jpeg, encode(edit(frame(), "local_text")))
        clock.call_later(.3, screen._on_jpeg, encode(edit(frame(), "noise")))
        try:
            with pytest.raises(RuntimeError, match="变化"):
                await phone.click_observed(**request, host_observation=observed)
            assert link.written == [] and clock.elapsed < 10.3
        finally:
            device.close()
    asyncio.run(scenario())
