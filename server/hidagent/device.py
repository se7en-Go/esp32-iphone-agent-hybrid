"""COM 口上的 `<id> <command>` 协议。来源是 Handsy hid_bridge.ino 的 USB 串口分支。

普通 HID 命令用递增 id。`netinfo` / `status` / `net` 必须用 id 0：固件按整行
`0 netinfo`、`0 status`、`0 net ` 做了快捷分支，vtok 只在 `0 netinfo` 里。
回复里的 Wi-Fi 密码不会写进日志，也不会留在对象上。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

import serial
from serial.tools import list_ports

from . import messages

log = logging.getLogger(__name__)

WCH_VID = 0x1A86
# CH343 是 0x55D3。克隆板有时报成 CH340 / CH9102，所以同一厂家的这几个 PID 都接受。
WCH_PIDS = {0x55D3, 0x55D4, 0x7523, 0x5523}
# 打开串口后有的克隆板会复位，setup() 要几秒。同一句柄上每隔 300ms 再问一次，不要反复开关口。
PING_EVERY = 0.3
PING_WINDOW = 8.0
HID_COMMANDS = {"home", "move", "press", "release", "click", "scroll", "type", "key", "homescreen"}
FIXED_ID = {"netinfo", "status", "net"}


class DeviceError(RuntimeError):
    pass


def open_serial(port: str, baud: int = 115200, impl=None):
    """DTR/RTS 保持不复位。先赋值再 open，避免 pyserial 默认把线拉下去触发 EN。"""
    ser = impl if impl is not None else serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    if not getattr(ser, "is_open", False):
        ser.open()
    ser.dtr = False
    ser.rts = False
    return ser


class SerialLink:
    """阻塞的 pyserial 收成一行一行的异步队列。"""

    def __init__(self, port: str, baud: int = 115200, opened=None):
        self._ser = opened if opened is not None else open_serial(port, baud)
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buf = bytearray()
        self._closed = False
        self._thread = threading.Thread(target=self._pump, name=f"serial-{port}", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        while not self._closed:
            try:
                data = self._ser.read(256)
            except Exception:
                break
            if not data:
                continue
            self._buf.extend(data)
            while True:
                nl = self._buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(self._buf[:nl + 1])
                del self._buf[:nl + 1]
                self._post(line)
        self._post(b"")

    def _post(self, line: bytes) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, line)
        except RuntimeError:
            return

    def write(self, data: bytes) -> None:
        if self._closed:
            raise OSError("serial closed")
        self._ser.write(data)
        self._ser.flush()

    async def readline(self) -> bytes:
        return await self._queue.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ser.close()
        except Exception:
            pass
        thread = getattr(self, "_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def is_open(self) -> bool:
        return not self._closed and bool(getattr(self._ser, "is_open", False))


def port_is_ch343(port) -> bool:
    """必须同时是 VID 0x1A86 和已知 PID（含 CH343 的 0x55D3）。

    厂家名大小写不敏感，正确拼写是 qinheng。只出现这个名字、没有这对 VID/PID，不能放行。
    厂家名按小写比较，正确拼写是 qinheng。只有这个名字、没有 VID/PID，不放行。
    """
    return getattr(port, "vid", None) == WCH_VID and getattr(port, "pid", None) in WCH_PIDS


def candidate_ports() -> list[str]:
    """只列出沁恒 USB 串口。不要去打开 /dev/ttyS0 这种本机串口。"""
    return [port.device for port in list_ports.comports() if port_is_ch343(port)]


class Device:
    """一次只发一条命令。`links` 在每次接上时加一，手势用它发现该重新 home。"""

    def __init__(self):
        self._link = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._last = self._beat = 0.0
        self.links = 0
        self.port_name = ""
        self.ip = ""
        self.mode = ""
        self.ssid = ""
        self.vtok = ""
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return self._link is not None and self._link.is_open()

    def attach(self, link, port_name: str = "") -> None:
        previous = self._link
        self._link = link
        if port_name:
            self.port_name = port_name
        self.links += 1
        self._last = self._beat = time.time()
        if previous is not None and previous is not link:
            previous.close()

    def close(self) -> None:
        link = self._link
        self._link = None
        if link is not None:
            link.close()

    @staticmethod
    def _timeout(command: str) -> float:
        """板子应答可能要一两秒；move 按距离，type 按字符。和原来的 TCP 设备同一套上限。"""
        name, _, arg = command.partition(" ")
        if name == "move":
            nums = [abs(int(n)) for n in arg.split() if _is_int(n)]
            span = max(nums) if nums else 0
            return 5 + span / 10 * 0.012
        if name == "type":
            return 5 + 0.1 * len(arg)
        if name == "ping":
            return 8
        return 5

    def _request_id(self, command: str) -> str:
        name = command.split(" ", 1)[0]
        if name in FIXED_ID:
            return "0"
        self._seq = self._seq % 100000 + 1
        return str(self._seq)

    async def _read_reply(self, link, req_id: str, timeout: float) -> str:
        prefix = req_id + " "
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(link.readline(), remaining)
            if not raw:
                raise OSError("serial closed")
            text = raw.decode("utf-8", "replace").strip()
            if text == req_id:
                return ""
            if text.startswith(prefix):
                return text[len(prefix):]

    async def exchange(self, command: str) -> str:
        if not self.connected:
            raise DeviceError(messages.BOARD_NOT_CONNECTED)
        timeout = self._timeout(command)
        async with self._lock:
            link = self._link
            if link is None or not link.is_open():
                raise DeviceError(messages.BOARD_NOT_CONNECTED)
            req_id = self._request_id(command)
            try:
                link.write(f"{req_id} {command}\n".encode())
                reply = await self._read_reply(link, req_id, timeout)
            except asyncio.CancelledError:
                link.close()
                if self._link is link:
                    self._link = None
                raise
            except (OSError, asyncio.TimeoutError) as exc:
                link.close()
                if self._link is link:
                    self._link = None
                raise DeviceError(messages.link_lost(command)) from exc
        self._beat = time.time()
        if command.split(" ", 1)[0] in HID_COMMANDS:
            self._last = self._beat
        name = command.split(" ", 1)[0]
        if name not in ("ping", "net", "netinfo", "status"):
            log.debug("%s -> %s", name, reply[:80])
        return reply

    async def query(self, command: str) -> str:
        return await self.exchange(command)

    async def send(self, command: str) -> None:
        reply = await self.exchange(command)
        if reply != "ok":
            raise DeviceError(messages.explain_reply(command, reply))

    async def refresh_netinfo(self) -> dict:
        body = await self.query("netinfo")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise DeviceError("netinfo 不是 JSON。请确认固件是本仓库编译的 Handsy 桥，而不是空白芯片。") from exc
        if not isinstance(data, dict):
            raise DeviceError("netinfo 的格式不对。")
        self.ip = str(data.get("ip") or "")
        self.mode = str(data.get("mode") or "")
        self.ssid = str(data.get("ssid") or "")
        self.vtok = str(data.get("vtok") or "")
        return {key: value for key, value in data.items() if key != "pass"}

    async def configure_network(self, mode: str, ssid: str = "", password: str = "") -> None:
        if mode == "ap":
            payload: dict = {"mode": "ap"}
        elif mode == "off":
            payload = {"mode": "off"}
        elif mode == "sta":
            if not ssid:
                raise DeviceError("STA 需要 2.4GHz 的 Wi-Fi 名称。")
            payload = {"mode": "sta", "ssid": ssid, "pass": password}
        else:
            raise DeviceError("网络模式只能是 sta、ap 或 off。")
        line = "net " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await self.send(line)

    async def connect(self, port: str | None = None) -> None:
        if self.connected:
            return
        name = (port or os.environ.get("HIDAGENT_PORT") or "").strip() or await discover_port()
        try:
            link = SerialLink(name)
        except serial.SerialException as exc:
            self.last_error = messages.port_open_failed(name)
            raise DeviceError(self.last_error) from exc
        self.attach(link, name)
        try:
            ready = await wait_for_ping(link, PING_WINDOW)
        except OSError as exc:
            self.close()
            self.last_error = messages.link_lost("ping")
            raise DeviceError(self.last_error) from exc
        if not ready:
            self.close()
            self.last_error = messages.PORT_SILENT
            raise DeviceError(self.last_error)
        self.last_error = ""
        log.info("board serial ready on %s", name)

    async def maintain(self) -> None:
        """掉线就重连，连上就刷新 IP 和 vtok。密码不留下。"""
        while True:
            try:
                if not self.connected:
                    await self.connect()
                await self.refresh_netinfo()
                self.last_error = ""
            except DeviceError as exc:
                self.last_error = str(exc)
                log.info("%s", exc)
            except Exception as exc:
                self.last_error = messages.BOARD_NOT_CONNECTED
                log.warning("board maintain: %s", type(exc).__name__)
            await asyncio.sleep(2)

    async def wait_connected(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while not self.connected and time.time() < deadline:
            await asyncio.sleep(0.2)
        return self.connected

    async def keep_awake(self, idle: float = 25, heartbeat: float = 2, screen=None) -> None:
        """netinfo 也会刷新 _beat，所以不能等“完全没流量”才去碰指针，否则手机会自己锁屏。"""
        while True:
            await asyncio.sleep(heartbeat)
            if not self.connected:
                continue
            try:
                if time.time() - self._last > idle and (screen is None or screen.live):
                    await self.send("move 1 0")
                    await self.send("move -1 0")
                elif time.time() - self._beat >= heartbeat:
                    await self.send("ping")
            except DeviceError as exc:
                log.info("board gone: %s", exc)


def _is_int(text: str) -> bool:
    try:
        int(text)
    except ValueError:
        return False
    return True


async def wait_for_ping(link, timeout: float = PING_WINDOW) -> bool:
    """同一句柄上反复发 `0 ping`。启动日志不是应答，丢掉，直到看到 `0 ok`。"""
    deadline = time.monotonic() + timeout
    next_ping = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_ping:
            link.write(b"0 ping\n")
            next_ping = now + PING_EVERY
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(link.readline(), min(PING_EVERY, remaining))
        except asyncio.TimeoutError:
            continue
        if not raw:
            return False
        if raw.decode("utf-8", "replace").strip() == "0 ok":
            return True
    return False


async def probe_port(name: str, timeout: float = PING_WINDOW) -> bool:
    try:
        link = SerialLink(name)
    except serial.SerialException:
        return False
    try:
        return await wait_for_ping(link, timeout)
    except OSError:
        return False
    finally:
        link.close()


async def discover_port() -> str:
    forced = os.environ.get("HIDAGENT_PORT", "").strip()
    if forced:
        return forced
    ports = candidate_ports()
    if not ports:
        raise DeviceError(messages.NO_PORT)
    for name in ports:
        if await probe_port(name):
            return name
    raise DeviceError(messages.PORT_SILENT + " 看到的口：" + ", ".join(ports))
