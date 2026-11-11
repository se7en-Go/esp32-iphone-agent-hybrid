import asyncio
import threading
import time

import pytest

from hidagent.device import (
    WCH_VID,
    Device,
    DeviceError,
    SerialLink,
    open_serial,
    port_is_ch343,
    wait_for_ping,
)
from hidagent.messages import NO_PORT
from hidagent.phone import Phone


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


class _HoldFirst:
    def __init__(self):
        self.written: list[bytes] = []
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False
        self.first_written = asyncio.Event()
        self.release = asyncio.Event()
        self._count = 0

    def write(self, data: bytes) -> None:
        self.written.append(data)
        req = data.decode().strip().split()[0]
        self._count += 1
        if self._count == 1:
            self.first_written.set()

            async def later(req_id: str = req) -> None:
                await self.release.wait()
                await self.replies.put(f"{req_id} ok\n".encode())

            asyncio.get_running_loop().create_task(later())
        else:
            self.replies.put_nowait(f"{req} ok\n".encode())

    async def readline(self) -> bytes:
        return await self.replies.get()

    def close(self) -> None:
        self.closed = True
        self.replies.put_nowait(b"")

    def is_open(self) -> bool:
        return not self.closed


def test_stop_aborts_remaining_type_chunks_when_tripped_mid_ack(tmp_path):
    async def scenario():
        device = Device()
        link = _HoldFirst()
        device.attach(link)
        tripped = {"on": False}

        def gate(command: str) -> None:
            name = command.split(" ", 1)[0]
            if tripped["on"] and name not in {"release", "home"}:
                raise DeviceError("熔断")

        device.gate = gate
        phone = Phone(device, tmp_path / "calibration.json", None)
        job = asyncio.create_task(phone.type("x" * 1000))
        await asyncio.wait_for(link.first_written.wait(), 1)
        tripped["on"] = True
        device.request_abort()
        link.release.set()
        with pytest.raises((DeviceError, asyncio.CancelledError)):
            await job
        typed = [line for line in link.written if b" type " in line]
        assert len(typed) == 1
        await device.send("release")
        assert any(b" release" in line for line in link.written)
        before = len(link.written)
        with pytest.raises(DeviceError):
            await device.send("type more")
        assert len(link.written) == before

    asyncio.run(scenario())


class FakeSerial:
    """打开时吐一行启动日志，前几次 ping 不答，模拟克隆板一开串口就复位。"""

    def __init__(self, replies_after: int = 3):
        self.port = ""
        self.baudrate = 115200
        self.timeout = 0.2
        self.dtr = True
        self.rts = True
        self.is_open = False
        self.opens = 0
        self.pings = 0
        self.replies_after = replies_after
        self._buf = bytearray()
        self._lock = threading.Lock()

    def open(self) -> None:
        self.opens += 1
        self.is_open = True
        with self._lock:
            self._buf.extend(b"rst:0x1 (POWERON_RESET),boot:0x13\r\n")

    def close(self) -> None:
        self.is_open = False

    def write(self, data: bytes) -> None:
        if b"0 ping" in data:
            self.pings += 1
            if self.pings >= self.replies_after:
                with self._lock:
                    self._buf.extend(b"0 ok\n")

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        time.sleep(0.01)
        with self._lock:
            if not self._buf:
                return b""
            chunk = bytes(self._buf[:size])
            del self._buf[:len(chunk)]
            return chunk


def test_ping_stays_on_one_handle_and_skips_the_boot_log():
    fake = FakeSerial(replies_after=3)

    async def scenario():
        opened = open_serial("COM9", impl=fake)
        assert fake.opens == 1
        assert fake.dtr is False and fake.rts is False
        link = SerialLink("COM9", opened=opened)
        try:
            assert await wait_for_ping(link, timeout=2)
        finally:
            link.close()

    asyncio.run(scenario())
    assert fake.opens == 1
    assert fake.pings >= 3


def test_ch343_needs_wch_vid_and_a_known_pid():
    class Port:
        def __init__(self, **fields):
            self.device = fields.pop("device")
            self.vid = fields.get("vid")
            self.pid = fields.get("pid")
            self.manufacturer = fields.get("manufacturer", "")

    assert port_is_ch343(Port(device="COM5", vid=WCH_VID, pid=0x55D3, manufacturer="QinHeng"))
    assert port_is_ch343(Port(device="COM6", vid=WCH_VID, pid=0x55D4, manufacturer=""))
    assert not port_is_ch343(Port(device="COM7", vid=WCH_VID, pid=0x9999, manufacturer="Qinheng"))
    assert not port_is_ch343(Port(device="/dev/ttyS0", vid=None, pid=None, manufacturer="qinheng"))
    assert not port_is_ch343(Port(device="COM8", vid=0x1234, pid=0x55D3, manufacturer="wch"))


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
