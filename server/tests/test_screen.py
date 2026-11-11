import asyncio
import io
import time

from PIL import Image

from hidagent.screen import Screen


def frame(color):
    buffer = io.BytesIO()
    Image.new("RGB", (100, 200), color).save(buffer, "JPEG")
    return buffer.getvalue()


def test_repeated_identical_video_frames_do_not_force_maximum_wait():
    async def scenario():
        screen = Screen()
        screen.frame = frame("white")

        async def duplicate_frames():
            while True:
                screen.updated = time.time()
                screen.frame = bytes(bytearray(screen.frame))
                await asyncio.sleep(.01)

        task = asyncio.create_task(duplicate_frames())
        try:
            started = time.monotonic()
            await screen.settled(quiet=.05, least=.01, most=.3)
            assert .04 <= time.monotonic() - started < .2
        finally:
            task.cancel()
    asyncio.run(scenario())


def test_real_content_change_restarts_quiet_window():
    async def scenario():
        screen = Screen()
        screen.frame = frame("white")

        async def change():
            await asyncio.sleep(.03)
            screen.frame = frame("black")
            screen.updated = time.time()

        task = asyncio.create_task(change())
        started = time.monotonic()
        await screen.settled(quiet=.06, least=.01, most=.4)
        assert time.monotonic() - started >= .10
        await task
    asyncio.run(scenario())


def test_animation_still_obeys_maximum_wait():
    async def scenario():
        screen = Screen()
        screen.frame = frame("white")

        async def animate():
            # A black/white toggle can land back on the frame last sampled.
            # Windows sleeps in ~15.6ms ticks, so the 40ms flip and the 50ms
            # poll often take an even number of flips between samples. settled
            # then treats the screen as quiet and returns at quiet (0.15s).
            # The 0.172s failure is that path, not a 0.2s maximum measured
            # short: the same monotonic clock cannot take the max path before
            # most. A new, obviously different image each step is still a
            # change no matter how many steps fall between samples.
            n = 0
            while True:
                n += 1
                screen.frame = frame(((n * 97) % 256, (n * 57) % 256, 255 - (n * 23) % 256))
                screen.updated = time.time()
                await asyncio.sleep(.02)

        task = asyncio.create_task(animate())
        try:
            started = time.monotonic()
            await screen.settled(quiet=.15, least=.01, most=.2)
            # Stay above the quiet window. 0.19 - 0.05 would accept the 0.172s return.
            assert .19 <= time.monotonic() - started < .4
        finally:
            task.cancel()
    asyncio.run(scenario())
