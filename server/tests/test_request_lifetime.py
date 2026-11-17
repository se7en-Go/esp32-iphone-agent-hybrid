"""HTTP completion must not depend on a receive wrapper preserving cancellation."""

import asyncio

from hidagent.request_lifetime import while_connected


def test_completed_work_stops_receiver_when_cancel_is_swallowed():
    async def scenario():
        entered = asyncio.Event()

        class Request:
            calls = 0

            async def receive(self):
                self.calls += 1
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if self.calls != 1:
                        raise
                    # Python 3.10 AnyIO can swallow an external cancellation
                    # when its receive task group cancels itself concurrently.
                    return {"type": "http.request", "body": b"", "more_body": False}

        async def work():
            await entered.wait()
            return "completed"

        request = Request()
        result = await asyncio.wait_for(while_connected(request, work()), .1)
        assert result == "completed"
        assert request.calls == 1

    asyncio.run(scenario())
