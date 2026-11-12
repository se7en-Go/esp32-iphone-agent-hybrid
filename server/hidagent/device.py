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
import sys
import threading
import time
from contextlib import nullcontext

import serial
from serial.tools import list_ports

from . import messages

log = logging.getLogger(__name__)

# COM 口上常见的 USB 转串口芯片。这里是自动识别的唯一名单。
# 不要把乐鑫原生 USB（VID 0x303A，USB-JTAG / HID）算进去，那个口是给 iPhone 的。
WCH_VID = 0x1A86
# CH343 是 0x55D3。克隆板有时报成 CH340 / CH9102，所以同一厂家的这几个 PID 都接受。
WCH_PIDS = {0x55D3, 0x55D4, 0x7523, 0x5523}
FTDI_VID = 0x0403
FTDI_PIDS = {0x6001, 0x6010, 0x6011, 0x6014, 0x6015}
CP210X_VID = 0x10C4
CP210X_PIDS = {0xEA60}
ESPRESSIF_VID = 0x303A
COM_BRIDGES = {
    WCH_VID: WCH_PIDS,
    FTDI_VID: FTDI_PIDS,
    CP210X_VID: CP210X_PIDS,
}
# 打开串口后有的克隆板会复位，setup() 要几秒。同一句柄上每隔 300ms 再问一次，不要反复开关口。
PING_EVERY = 0.3
PING_WINDOW = 8.0
HID_COMMANDS = {"home", "move", "press", "release", "click", "scroll", "type", "key", "homescreen"}
FIXED_ID = {"netinfo", "status", "net"}


class DeviceError(RuntimeError):
    pass


def open_serial(port: str, baud: int = 115200, impl=None, platform: str | None = None):
    """打开 COM 口但不复位板子（打开串口会重启 ESP32，AirPlay 镜像就断了）。

    板子的自动下载电路：RTS 有效、DTR 无效时把 EN 拉低，板子复位。
    - macOS / Linux：内核在 open() 时同时拉起 DTR 和 RTS，pyserial 随后先放 DTR、再放 RTS，
      中间那一下正好是 DTR 无效、RTS 有效，FTDI FT232R 的板子每次都会复位。
      所以 open 前让 DTR 保持有效、RTS 无效：pyserial 只放 RTS，open 之后再放 DTR，全程不出现复位组合。
    - Windows：两根线由同一个 DCB 一次设好，open 前都设成无效即可。
    硬件流控（rtscts / dsrdtr）关掉，免得驱动替我们动这两根线。esptool 刷机自己负责复位，不走这里。
    """
    ser = impl if impl is not None else serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.2
    ser.rtscts = False
    ser.dsrdtr = False
    ser.rts = False
    ser.dtr = (platform or sys.platform) != "win32"
    if not getattr(ser, "is_open", False):
        ser.open()
    ser.rts = False  # RTS first: DTR low with RTS still high is the reset pulse
    ser.dtr = False
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


def port_is_com_bridge(port) -> bool:
    """COM 口上的 USB 转串口：沁恒、FTDI、CP210x。不要认乐鑫 0x303A，也不要认没有 VID 的本机口。"""
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    if vid == ESPRESSIF_VID or vid is None or pid is None:
        return False
    return pid in COM_BRIDGES.get(vid, ())


def candidate_ports() -> list[str]:
    """只列出 COM 口上的 USB 转串口。不要去打开 /dev/ttyS0，也不要打开乐鑫原生 USB。"""
    return [port.device for port in list_ports.comports() if port_is_com_bridge(port)]


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
        self.board_status: dict = {}
        self.pace_version: int | None = None
        self._abort = False
        self._abort_epoch = 0
        self._closing = False
        self._exchanges: set[asyncio.Task] = set()
        self.maintain_task: asyncio.Task | None = None

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

    def unguarded(self):
        return _Unguarded(self)

    @property
    def abort_epoch(self) -> int:
        return self._abort_epoch

    def request_abort(self) -> None:
        """紧急停止。只打断正在等应答的那一次交换，不取消维护任务本身。"""
        self._abort = True
        self._abort_epoch += 1
        for task in list(self._exchanges):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()

    def clear_abort(self) -> None:
        self._abort = False

    async def exchange(self, command: str) -> str:
        if not self.connected:
            raise DeviceError(messages.BOARD_NOT_CONNECTED)
        timeout = self._timeout(command)
        name = command.split(" ", 1)[0]
        async with self._lock:
            # asyncio 锁只串行化交换；熔断还需要 gate 提供的跨线程写入锁。
            if self._abort and name not in {"release", "home"}:
                raise DeviceError("已紧急停止，后面的内容没有发出。")
            gate = getattr(self, "gate", None)
            write_guard = gate(command) if gate is not None else None
            link = self._link
            if link is None or not link.is_open():
                raise DeviceError(messages.BOARD_NOT_CONNECTED)
            req_id = self._request_id(command)
            try:
                # 板子收到这一行之后才选 USB 或 BLE。两条路都先经过这把锁里的熔断复查，没有第二条写手机的路。
                with write_guard if write_guard is not None else nullcontext():
                    link.write(f"{req_id} {command}\n".encode())
                reply = await self._read_reply(link, req_id, timeout)
            except asyncio.CancelledError:
                if not self._abort:
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

    async def _await_exchange(self, command: str) -> str:
        """在子任务里等应答。紧急停止只取消这次交换，调用方（包括维护循环）还在。"""
        task = asyncio.create_task(self.exchange(command), name=f"exchange-{command.split(' ', 1)[0]}")
        self._exchanges.add(task)
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            # 3.10 不会跟着把子任务取消掉。这里自己关掉串口，下一句才不会读到半截回复。
            task.cancel()
            if not self._abort:
                link = self._link
                if link is not None:
                    link.close()
                    if self._link is link:
                        self._link = None
            raise
        finally:
            self._exchanges.discard(task)
        if task.cancelled():
            raise DeviceError("已紧急停止，后面的内容没有发出。")
        return task.result()

    async def query(self, command: str) -> str:
        return await self._await_exchange(command)

    async def send(self, command: str) -> None:
        reply = await self._await_exchange(command)
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

    async def refresh_hid_status(self) -> None:
        """Read `status`. The hid object carries the active transport and the pace number."""
        try:
            data = json.loads(await self.query("status"))
        except (DeviceError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.board_status = data
        hid = data.get("hid")
        pace = hid.get("pace") if isinstance(hid, dict) else None
        if type(pace) is int:
            self.pace_version = pace

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
        """掉线就重连，连上就刷新 IP 和 vtok。密码不留下。紧急停止不会结束这个循环。"""
        while not self._closing:
            try:
                if self._abort:
                    await asyncio.sleep(0.05)
                    continue
                if not self.connected:
                    await self.connect()
                await self.refresh_netinfo()
                await self.refresh_hid_status()
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except DeviceError as exc:
                self.last_error = str(exc)
                log.info("%s", exc)
            except Exception as exc:
                self.last_error = messages.BOARD_NOT_CONNECTED
                log.warning("board maintain: %s", type(exc).__name__)
            await asyncio.sleep(0.05 if self._abort else 2)

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


async def supervise_maintain(device: Device) -> None:
    """维护任务如果自己退出，再拉起来。关机时 _closing 为真，不再重启。"""
    while not device._closing:
        task = asyncio.create_task(device.maintain())
        device.maintain_task = task
        try:
            await task
        except asyncio.CancelledError:
            if device._closing:
                raise
        except Exception:
            log.warning("maintain exited", exc_info=True)
        if device._closing:
            return
        await asyncio.sleep(0.05)


class _Unguarded:
    def __init__(self, device: Device):
        self._device = device

    def __enter__(self):
        self._device._hid_bypass = getattr(self._device, "_hid_bypass", 0) + 1
        return self._device

    def __exit__(self, *_args):
        self._device._hid_bypass -= 1
        return False


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
