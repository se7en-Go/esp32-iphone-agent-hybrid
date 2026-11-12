import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from hidagent.device import (
    CP210X_VID,
    ESPRESSIF_VID,
    FTDI_PIDS,
    FTDI_VID,
    WCH_VID,
    Device,
    DeviceError,
    SerialLink,
    candidate_ports,
    open_serial,
    port_is_ch343,
    port_is_com_bridge,
    supervise_maintain,
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


@pytest.mark.parametrize("method", ["wait_connected", "keep_awake"])
def test_device_exposes_connection_lifecycle_methods(method):
    assert callable(getattr(Device(), method, None)), f"Device.{method} is missing"


def test_keep_awake_nudges_an_idle_connected_phone(monkeypatch):
    from hidagent import device as module

    async def scenario():
        device = Device()
        link = MemoryLink()
        device.attach(link)
        now = device._last + 30
        monkeypatch.setattr(module.time, "time", lambda: now)
        beats = 0

        async def heartbeat(_delay):
            nonlocal beats
            beats += 1
            if beats > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(module.asyncio, "sleep", heartbeat)
        firmware = asyncio.create_task(run_firmware(link))
        try:
            with pytest.raises(asyncio.CancelledError):
                await device.keep_awake()
            assert link.written == [b"1 move 1 0\n", b"2 move -1 0\n"]
        finally:
            firmware.cancel()
            await asyncio.gather(firmware, return_exceptions=True)
            device.close()

    asyncio.run(scenario())


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


def test_cancelled_raw_press_releases_and_ignores_late_ack():
    async def scenario():
        device = Device()
        link = MemoryLink()
        device.attach(link)
        job = asyncio.create_task(device.send("press"))
        press = await asyncio.wait_for(link.commands.get(), 1)
        job.cancel()
        release = await asyncio.wait_for(link.commands.get(), 1)
        assert release.endswith(b" release\n")
        assert device._lock.locked() and not link.closed
        # Neither the caller nor a repeated emergency stop may abandon cleanup.
        job.cancel()
        device.request_abort()
        link.replies.put_nowait((firmware_reply(press.decode().strip()) + "\n").encode())
        done, _ = await asyncio.wait({job}, timeout=0.02)
        assert not done, "the old press ACK completed release cleanup"
        assert device._lock.locked() and not link.closed
        link.replies.put_nowait((firmware_reply(release.decode().strip()) + "\n").encode())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 1)
        assert device.connected and not device._lock.locked()
        assert not device._exchanges
        assert link.written == [press, release]

        device.clear_abort()
        # A stale ACK must also not make a subsequent HID command succeed early.
        following = asyncio.create_task(device.send("move 1 0"))
        command = await asyncio.wait_for(link.commands.get(), 1)
        link.replies.put_nowait((firmware_reply(press.decode().strip()) + "\n").encode())
        done, _ = await asyncio.wait({following}, timeout=0.02)
        assert not done
        link.replies.put_nowait((firmware_reply(command.decode().strip()) + "\n").encode())
        await asyncio.wait_for(following, 1)
        assert device.connected

    asyncio.run(scenario())


def test_cancelled_press_release_without_ack_has_its_own_timeout(monkeypatch):
    from hidagent import device as module

    monkeypatch.setattr(module, "RELEASE_TIMEOUT", 0.03)

    async def scenario():
        device = Device()
        link = MemoryLink()
        device.attach(link)
        job = asyncio.create_task(device.send("press"))
        press = await asyncio.wait_for(link.commands.get(), 1)
        job.cancel()
        release = await asyncio.wait_for(link.commands.get(), 1)
        assert release.endswith(b" release\n")
        assert not link.closed, "UART closed before release was written"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 0.5)
        assert link.written == [press, release]
        assert not device._lock.locked()
        assert not device._exchanges

    asyncio.run(scenario())


def test_cancelled_drag_releases_while_background_query_holds_serial_lock(tmp_path, monkeypatch):
    from hidagent import phone as module

    monkeypatch.setattr(module, "HOLD", 60)
    monkeypatch.setattr(module, "RELEASE_TIMEOUT", 0.03)

    class ButtonLink(MemoryLink):
        pressed = False

        def write(self, data):
            super().write(data)
            command = data.decode().strip().split(" ", 1)[1]
            if command == "press":
                self.pressed = True
            elif command == "release":
                self.pressed = False

    async def scenario():
        device, link = Device(), ButtonLink()
        device.attach(link)
        phone = Phone(device, tmp_path / "calibration.json")
        phone.span = {"w": 500, "h": 1000}
        drag = asyncio.create_task(phone.drag([(.5, .5), (.7, .7)]))
        for expected in ["release", "home", "move 250 500", "press", "move 100 200"]:
            wire = await asyncio.wait_for(link.commands.get(), 1)
            assert wire.decode().strip().split(" ", 1)[1] == expected
            link.replies.put_nowait((firmware_reply(wire.decode().strip()) + "\n").encode())

        async def wait_for_hold():
            while phone._pos != (350, 700):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_hold(), 1)
        assert link.pressed
        query = asyncio.create_task(device.query("ping"))
        ping = await asyncio.wait_for(link.commands.get(), 1)
        assert ping.endswith(b" ping\n")
        assert device._lock.locked()
        before_cancel = len(link.written)
        drag.cancel()
        await asyncio.wait_for(asyncio.gather(drag, return_exceptions=True), 0.5)
        assert not link.pressed, "release was lost while queued behind a background query"
        assert not link.closed
        assert len(link.written) == before_cancel + 1
        release = await asyncio.wait_for(link.commands.get(), 1)
        assert release.endswith(b" release\n")
        # The existing query reader must ignore the emergency release's distinct ACK.
        link.replies.put_nowait((firmware_reply(release.decode().strip()) + "\n").encode())
        done, _ = await asyncio.wait({query}, timeout=0.02)
        assert not done
        link.replies.put_nowait((firmware_reply(ping.decode().strip()) + "\n").encode())
        assert await asyncio.wait_for(query, 1) == "ok"
        assert device.connected and not device._lock.locked()

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


class _NetinfoLink:
    def __init__(self):
        self.written: list[bytes] = []
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()
        self.first = asyncio.Event()
        self.second = asyncio.Event()
        self.netinfos = 0
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)
        text = data.decode()
        req = text.split()[0]
        if "netinfo" in text:
            self.netinfos += 1
            if self.netinfos == 1:
                self.first.set()
                return
            self.second.set()
            body = '{"ip":"10.0.0.2","vtok":"abcd"}'
            self.replies.put_nowait(f"{req} {body}\n".encode())
            return
        self.replies.put_nowait(f"{req} ok\n".encode())

    async def readline(self) -> bytes:
        return await self.replies.get()

    def close(self) -> None:
        self.closed = True
        self.replies.put_nowait(b"")

    def is_open(self) -> bool:
        return not self.closed


def test_stop_during_netinfo_leaves_maintain_alive_and_it_reconnects():
    async def scenario():
        device = Device()
        link = _NetinfoLink()
        device.attach(link)
        supervisor = asyncio.create_task(supervise_maintain(device))
        await asyncio.wait_for(link.first.wait(), 1)
        device.request_abort()
        await asyncio.sleep(0)
        await device.send("release")
        await device.send("home")
        device.clear_abort()
        assert device.maintain_task is not None and not device.maintain_task.done()
        await asyncio.wait_for(link.second.wait(), 1)
        assert not device.maintain_task.done()
        assert not supervisor.done()
        device._closing = True
        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor

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
        assert "FTDI" in str(caught.value)

    asyncio.run(scenario())
    assert "HIDAGENT_PORT" in NO_PORT


class _FakePort:
    def __init__(self, device, vid=None, pid=None, manufacturer=""):
        self.device = device
        self.vid = vid
        self.pid = pid
        self.manufacturer = manufacturer


def test_com_bridge_accepts_ftdi_and_cp210x_but_not_espressif():
    assert port_is_com_bridge(_FakePort("/dev/cu.usbserial-A5069RR4", FTDI_VID, 0x6001))
    for pid in sorted(FTDI_PIDS):
        assert port_is_com_bridge(_FakePort("COM8", FTDI_VID, pid))
    assert port_is_com_bridge(_FakePort("/dev/cu.SLAB_USBtoUART", CP210X_VID, 0xEA60))
    assert port_is_com_bridge(_FakePort("COM5", WCH_VID, 0x55D3))
    assert not port_is_com_bridge(_FakePort("/dev/cu.usbmodem14101", ESPRESSIF_VID, 0x1001))
    assert not port_is_com_bridge(_FakePort("/dev/ttyS0"))
    assert not port_is_com_bridge(_FakePort("/dev/cu.Bluetooth-Incoming-Port"))
    assert not port_is_com_bridge(_FakePort("COM9", FTDI_VID, 0x1234))
    assert not port_is_com_bridge(_FakePort("COM10", 0x1234, 0x5678))
    assert not port_is_ch343(_FakePort("/dev/cu.usbserial-A5069RR4", FTDI_VID, 0x6001))


def test_candidate_ports_lists_known_bridges_only(monkeypatch):
    ports = [
        _FakePort("/dev/cu.usbserial-A5069RR4", FTDI_VID, 0x6001),
        _FakePort("/dev/cu.SLAB_USBtoUART", CP210X_VID, 0xEA60),
        _FakePort("/dev/cu.usbmodem14101", ESPRESSIF_VID, 0x1001),
        _FakePort("/dev/cu.Bluetooth-Incoming-Port"),
        _FakePort("/dev/ttyS0"),
        _FakePort("COM5", WCH_VID, 0x55D3),
        _FakePort("COM9", FTDI_VID, 0x1234),
    ]
    monkeypatch.setattr("hidagent.device.list_ports.comports", lambda: ports)
    assert candidate_ports() == [
        "/dev/cu.usbserial-A5069RR4",
        "/dev/cu.SLAB_USBtoUART",
        "COM5",
    ]


RESET = (False, True)  # DTR released while RTS is held: the auto-program circuit pulls EN low


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX tty behaviour")
def test_posix_open_never_pulses_reset_through_real_pyserial(tmp_path):
    """FT232R on macOS: the tty raises DTR and RTS on open, then pyserial applies DTR before RTS."""
    import serial.serialposix

    class Lines(serial.serialposix.Serial):
        def open(self):
            self.history = [(True, True)]  # what the kernel does to both lines on open()
            super().open()

        def _reconfigure_port(self, force_update=False):
            pass

        def _record(self, **change):
            dtr, rts = self.history[-1]
            self.history.append((change.get("dtr", dtr), change.get("rts", rts)))

        def _update_dtr_state(self):
            self._record(dtr=self._dtr_state)

        def _update_rts_state(self):
            self._record(rts=self._rts_state)

    master, slave = os.openpty()
    try:
        plain = Lines()  # the old way: both off before open, which pyserial applies DTR first
        plain.port, plain.dtr, plain.rts = os.ttyname(slave), False, False
        plain.open()
        assert RESET in plain.history
        plain.close()

        ser = open_serial(os.ttyname(slave), impl=Lines(), platform="darwin")
        try:
            assert RESET not in ser.history, ser.history
            assert ser.history[-1] == (False, False) and not ser.rtscts and not ser.dsrdtr
        finally:
            ser.close()
    finally:
        os.close(master)
        os.close(slave)


def test_windows_open_sets_both_lines_off_at_once():
    class WinSerial(FakeSerial):
        def open(self):
            super().open()
            self.history = [(self.dtr, self.rts)]  # one DCB applies both states

    fake = open_serial("COM9", impl=WinSerial(), platform="win32")
    assert fake.history == [(False, False)] and fake.dtr is False and fake.rts is False
    assert fake.rtscts is False and fake.dsrdtr is False


def test_flashing_still_lets_esptool_reset_the_board():
    """flash.py relies on esptool's own DTR/RTS reset into the bootloader; nothing may turn that off."""
    text = (Path(__file__).resolve().parents[2] / "scripts" / "flash.py").read_text(encoding="utf-8")
    assert '"-m", "esptool"' in text and "open_serial" not in text
    assert "no_reset" not in text and "--before" not in text and "--after" not in text
