import asyncio

import pytest

from hidagent.device import Device, DeviceError
from hidagent.messages import NO_PORT


class MemoryLink:
    """测试用的假串口：write 进去的一行，由固件任务回一行。"""

    def __init__(self):
        self.commands: asyncio.Queue[bytes] = asyncio.Queue()
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        self.written.append(data)
        self.commands.put_nowait(data)

    async def readline(self) -> bytes:
        if self.closed and self.replies.empty():
            return b""
        return await self.replies.get()

    def close(self) -> None:
        self.closed = True
        self.replies.put_nowait(b"")

    def is_open(self) -> bool:
        return not self.closed


def firmware_reply(line: str) -> str:
    req_id, _, command = line.partition(" ")
    if command == "ping":
        return f"{req_id} ok"
    if command == "netinfo":
        return req_id + ' {"mode":"sta","ssid":"lab","pass":"secret-wifi","ip":"192.168.1.20","vtok":"ab"}'
    if command.startswith("net "):
        return f"{req_id} ok"
    if command == "bad":
        return f"{req_id} err unknown command"
    if "asleep" in command:
        return f"{req_id} err phone usb asleep (unlock the phone)"
    return f"{req_id} ok"


async def run_firmware(link: MemoryLink) -> None:
    while not link.closed:
        raw = await link.commands.get()
        if link.closed:
            return
        text = raw.decode().strip()
        link.replies.put_nowait((firmware_reply(text) + "\n").encode())


def test_timeout_grows_with_move_distance_and_text_length():
    assert Device._timeout("click") == 5
    assert Device._timeout("move 1000 -20") > Device._timeout("move 10 10") >= 5
    assert Device._timeout("type " + "x" * 40) > Device._timeout("type hi")


def test_send_without_board_fails_fast():
    with pytest.raises(DeviceError, match="没连上"):
        asyncio.run(Device().send("ping"))


def test_round_trip_strips_id_and_never_keeps_the_wifi_password():
    async def scenario():
        device = Device()
        link = MemoryLink()
        task = asyncio.create_task(run_firmware(link))
        device.attach(link, "COM7")
        await device.send("ping")
        info = await device.refresh_netinfo()
        assert info["ip"] == "192.168.1.20"
        assert info["vtok"] == "ab"
        assert "pass" not in info and "secret-wifi" not in info.values()
        assert device.mode == "sta" and device.ssid == "lab"
        await device.configure_network("sta", "lab", "secret-wifi")
        lines = [item.decode().strip() for item in link.written]
        assert lines[0].endswith("ping") and not lines[0].startswith("0 ")
        assert "0 netinfo" in lines
        assert any(line.startswith("0 net {") and "secret-wifi" in line for line in lines)
        task.cancel()
        link.close()

    asyncio.run(scenario())


def test_round_trip_reads_the_matching_id_and_ignores_noise():
    async def scenario():
        device = Device()
        link = MemoryLink()

        async def noisy():
            raw = await link.commands.get()
            req_id = raw.decode().strip().split()[0]
            link.replies.put_nowait(b"ets Jul 29 2019 boot\n")
            link.replies.put_nowait(f"{req_id} ok\n".encode())

        task = asyncio.create_task(noisy())
        device.attach(link, "COM3")
        await device.send("click")
        await task
        with pytest.raises(DeviceError, match="拒绝"):
            asyncio.create_task(run_firmware(link))
            await device.send("bad")
        link.close()

    asyncio.run(scenario())


def test_asleep_reply_tells_the_user_to_unlock():
    async def scenario():
        device = Device()
        link = MemoryLink()
        task = asyncio.create_task(run_firmware(link))
        device.attach(link)
        with pytest.raises(DeviceError, match="解锁"):
            await device.send("asleep")
        task.cancel()
        link.close()

    asyncio.run(scenario())


def test_failed_command_closes_only_the_link_it_used():
    async def scenario():
        device = Device()
        old, replacement = MemoryLink(), MemoryLink()
        started = asyncio.Event()

        async def stall(raw_link: MemoryLink):
            await raw_link.commands.get()
            started.set()
            await asyncio.Event().wait()

        asyncio.create_task(stall(old))
        device.attach(old, "COM1")
        pending = asyncio.create_task(device.send("ping"))
        await started.wait()
        device.attach(replacement, "COM2")
        with pytest.raises(DeviceError):
            await pending
        assert old.closed
        assert device.connected and not replacement.closed

    asyncio.run(scenario())


def test_cancelled_command_closes_the_link_before_the_next_command():
    async def scenario():
        device = Device()
        link = MemoryLink()
        started = asyncio.Event()

        async def stall():
            await link.commands.get()
            started.set()
            await asyncio.Event().wait()

        asyncio.create_task(stall())
        device.attach(link)
        job = asyncio.create_task(device.send("press"))
        await started.wait()
        queued = asyncio.create_task(device.send("click"))
        await asyncio.sleep(0)  # 下一条已经在等锁
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not device.connected
        with pytest.raises(DeviceError, match="没连上"):
            await asyncio.wait_for(queued, 0.5)

    asyncio.run(scenario())


def test_discover_without_a_wch_port_explains_the_next_step(monkeypatch):
    monkeypatch.setattr("hidagent.device.candidate_ports", lambda: [])
    monkeypatch.delenv("HIDAGENT_PORT", raising=False)

    async def scenario():
        with pytest.raises(DeviceError, match="没找到串口") as caught:
            await Device().connect()
        assert "COM" in str(caught.value)
        assert "CH343" in str(caught.value)

    asyncio.run(scenario())
    assert "HIDAGENT_PORT" in NO_PORT
