"""Reproducible local startup. Receiver and worker have independent lifetimes."""

import argparse
import fcntl
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .airplay_receiver import read_snapshot

ROOT = Path(__file__).resolve().parents[2]


def load_env(env, path):
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        key, separator, value = line.partition("=")
        if separator and key and not line.startswith("#"):
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def runtime_environment(source=None):
    env = dict(os.environ if source is None else source)
    for path in (ROOT / ".env", ROOT / "server/.env"):
        load_env(env, path)
    data = Path(env.setdefault("HIDAGENT_DATA", str(ROOT / "server/.local-data"))).expanduser().resolve()
    env["HIDAGENT_DATA"] = str(data)
    load_env(env, data / "runtime.env")
    env.setdefault("HOST", "127.0.0.1")
    env.setdefault("PORT", "8890")
    bundle = Path(env.get("UXPLAY_RUNTIME", str(ROOT / "server/.local-data/uxplay-runtime")))
    env.setdefault("SCREEN_BACKEND", "uxplay" if sys.platform == "darwin" and (bundle / "bin/uxplay").is_file()
                   else "board")
    if env["SCREEN_BACKEND"] == "uxplay":
        env.setdefault("AIRPLAY_NAME", "ESP32 Agent Mac")
        env.setdefault("AIRPLAY_SOCKET", str(data / "airplay.sock"))
        config = data / "uxplay.conf"
        if config.is_file():
            env.setdefault("UXPLAYRC", str(config))
        if (bundle / "bin/uxplay").is_file():
            env["PATH"] = os.pathsep.join((str(bundle / "bin"), str(bundle / "env/bin"), env.get("PATH", "")))
            env.setdefault("GST_PLUGIN_PATH", str(bundle / "plugins"))
            env.setdefault("GST_PLUGIN_SYSTEM_PATH", "")
            env.setdefault("GST_PLUGIN_SCANNER", str(bundle / "env/libexec/gstreamer-1.0/gst-plugin-scanner"))
            env.setdefault("GST_REGISTRY", str(bundle / "gst-registry-minimal.bin"))
    return env


def clipboard_proxy_config(env):
    """Validate before spawning any service; return listener settings without pairing credentials."""
    base = env.get("HIDAGENT_CLIPBOARD_BASE_URL", "").strip()
    key = env.get("HIDAGENT_CLIPBOARD_KEY", "").strip()
    trigger = env.get("HIDAGENT_CLIPBOARD_TRIGGER_KEY", "").strip() or None
    if not base and not key and trigger is None:
        return None
    if not base or not key:
        raise ValueError("剪贴板配置需要同时设置 HIDAGENT_CLIPBOARD_BASE_URL 和 HIDAGENT_CLIPBOARD_KEY。")
    from .clipboard import CompanionClipboard
    try:
        ttl = float(env.get("HIDAGENT_CLIPBOARD_TTL", "60"))
    except ValueError as exc:
        raise ValueError("HIDAGENT_CLIPBOARD_TTL 需要在 1..300 秒之间。") from exc
    CompanionClipboard(base, key, ttl, trigger_key=trigger,
                       protocol=env.get("HIDAGENT_CLIPBOARD_PROTOCOL", "legacy-v1").strip())
    managed = env.get("HIDAGENT_CLIPBOARD_MANAGED_PROXY", "1")
    if managed not in {"0", "1"}:
        raise ValueError("HIDAGENT_CLIPBOARD_MANAGED_PROXY 只能是 0 或 1。")
    if managed == "0":
        return {"managed": False}
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.path not in {"", "/"}:
        raise ValueError("受管剪贴板代理需要无路径的 http URL；"
                         "外置 HTTPS 代理请设置 HIDAGENT_CLIPBOARD_MANAGED_PROXY=0。")
    port = parsed.port if parsed.port is not None else 80
    control_port = int(env.get("PORT", "8890"))
    if not (1 <= port <= 65535 and 1 <= control_port <= 65535) or port == control_port:
        raise ValueError("剪贴板代理端口须为 1..65535 且不能等于控制端口；请使用显式专用端口。")
    host = env.get("HIDAGENT_CLIPBOARD_PROXY_HOST", "::" if ":" in parsed.hostname else "0.0.0.0")
    if host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("HIDAGENT_CLIPBOARD_PROXY_HOST 需要监听 IP 地址或 localhost。") from exc
    return {"managed": True, "host": host, "port": port, "control_port": control_port}


def _proxy_accepting(config):
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(config["host"], config["host"])
    if ":" in host:
        host = f"[{host}]"
    url = f"http://{host}:{config['port']}/api/clipboard/pending"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=.5):
            return False  # The paired endpoint must not accept an unauthenticated request.
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except (OSError, ValueError, urllib.error.URLError):
        return False


def serve_clipboard_proxy(fd, control_port):
    """Serve only the supervisor's inherited listener, never bind a second port."""
    import uvicorn

    from .clipboard_proxy import create_app

    with socket.socket(fileno=fd) as listener:
        # The supervisor already bound/listened before passing this owned fd.
        # SO_ACCEPTCONN exists in Python on some Macs but getsockopt rejects it.
        if (listener.family not in (socket.AF_INET, socket.AF_INET6)
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                or not listener.getsockname()[1]):
            raise RuntimeError("clipboard proxy requires an inherited TCP listening socket")
        config = uvicorn.Config(create_app(control_port=control_port), access_log=False, proxy_headers=False)
        uvicorn.Server(config).run(sockets=[listener])


def _worker_record(data):
    try:
        record = json.loads((data / "worker.json").read_text())
        token = record.get("instance")
        if (type(record.get("pid")) is int and record["pid"] > 1 and isinstance(token, str)
                and len(token) == 32 and all(char in "0123456789abcdef" for char in token)):
            return record
    except (OSError, ValueError):
        pass
    return None


def _owned_worker(record):
    if record is None:
        return False
    # An instance token in the managed supervisor's argv protects against reused PIDs.
    result = subprocess.run(["ps", "-p", str(record["pid"]), "-o", "command="],
                            capture_output=True, text=True, timeout=2)
    marker = f"-m hidagent.local_runtime worker --instance {record['instance']}"
    return result.returncode == 0 and marker in result.stdout


def receiver_status(env):
    if env["SCREEN_BACKEND"] != "uxplay":
        return None
    try:
        metadata, _ = read_snapshot(env["AIRPLAY_SOCKET"])
        return {key: metadata[key] for key in ("instance", "pid", "running", "live")}
    except (OSError, ValueError):
        return None


def worker_status(env):
    url = f"http://127.0.0.1:{int(env['PORT'])}/api/status"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=1) as response:
            state = json.load(response)
        return {key: state.get(key) for key in ("device", "screen", "calibrated", "ready")}
    except (OSError, ValueError, urllib.error.URLError):
        return None


def status(env):
    record = _worker_record(Path(env["HIDAGENT_DATA"]))
    try:
        config = clipboard_proxy_config(env)
        clipboard = {"configured": config is not None, **(config or {})}
        if config and config["managed"]:
            clipboard["running"] = bool(_owned_worker(record) and record.get("clipboard_proxy_ready")
                                        and _proxy_accepting(config))
    except ValueError as exc:
        clipboard = {"configured": False, "error": str(exc)}
    return {"worker_managed": _owned_worker(record), "worker": worker_status(env),
            "receiver": receiver_status(env), "backend": env["SCREEN_BACKEND"],
            "url": f"http://{env['HOST']}:{int(env['PORT'])}", "clipboard_proxy": clipboard}


@contextmanager
def management_lock(data):
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(data / "runtime.lock", "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _start_process(command, env, filename):
    data = Path(env["HIDAGENT_DATA"])
    descriptor = os.open(data / filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "ab", buffering=0) as log:
        return subprocess.Popen(command, cwd=ROOT / "server", env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)


def ensure_receiver(env):
    if env["SCREEN_BACKEND"] != "uxplay":
        return
    current = receiver_status(env)
    if current is not None and current["running"]:
        return
    if current is not None:
        raise RuntimeError("接收器正在恢复，请稍后检查 status；未启动重复实例。")
    if shutil.which("uxplay", path=env.get("PATH")) is None:
        raise RuntimeError("找不到 UxPlay；配置 UXPLAY_RUNTIME 或把 uxplay 放到 PATH。")
    config = env.get("UXPLAYRC")
    if config and not Path(config).is_file():
        raise RuntimeError("UXPLAYRC 指向的配置不存在，请使用持久的本地配置文件。")
    process = _start_process([sys.executable, "-m", "hidagent.airplay_receiver", "--socket",
                              env["AIRPLAY_SOCKET"], "--name", env["AIRPLAY_NAME"],
                              "--instance", uuid.uuid4().hex], env, "receiver.log")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        current = receiver_status(env)
        if current and current["running"]:
            return
        if process.poll() is not None:
            break
        time.sleep(.1)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=3)
    raise RuntimeError("接收器启动失败；请查看本地 receiver.log。")


def start_worker(env):
    proxy_config = clipboard_proxy_config(env)
    data = Path(env["HIDAGENT_DATA"])
    if _owned_worker(_worker_record(data)):
        return
    if worker_status(env) is not None:
        raise RuntimeError("端口上已有未由此脚本管理的服务，请先完成现有服务迁移。")
    token = uuid.uuid4().hex
    process = _start_process([sys.executable, "-m", "hidagent.local_runtime", "worker", "--instance", token],
                             env, "worker.log")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        record = _worker_record(data)
        if (worker_status(env) is not None and _owned_worker(record)
                and (not proxy_config or not proxy_config["managed"] or record.get("clipboard_proxy_ready"))):
            return
        if process.poll() is not None:
            break
        time.sleep(.1)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=12)
    raise RuntimeError("网页服务启动失败；请查看本地 worker.log。")


def stop_worker(env):
    data = Path(env["HIDAGENT_DATA"])
    record = _worker_record(data)
    if not _owned_worker(record):
        return
    os.kill(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 12
    while _owned_worker(record) and time.monotonic() < deadline:
        time.sleep(.1)
    if _owned_worker(record):
        raise RuntimeError("网页服务仍在关闭；保留接收器，请稍后检查 status。")


def stop_receiver(env):
    before = receiver_status(env)
    if before is None:
        return
    # Re-read the live socket immediately before signalling the same daemon instance.
    current = receiver_status(env)
    if current is None or current["instance"] != before["instance"] or current["pid"] != before["pid"]:
        raise RuntimeError("镜像接收器实例已变化，请重新检查 status。")
    result = subprocess.run(["ps", "-p", str(current["pid"]), "-o", "command="],
                            capture_output=True, text=True, timeout=2)
    if ("-m hidagent.airplay_receiver --socket " not in result.stdout
            or f"--instance {current['instance']}" not in result.stdout):
        raise RuntimeError("接收器进程身份未确认，未停止。")
    os.kill(current["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        after = receiver_status(env)
        if after is None or after["instance"] != current["instance"]:
            return
        time.sleep(.1)
    raise RuntimeError("接收器仍在关闭，请稍后检查 status。")


def worker(env, instance):
    proxy_config = clipboard_proxy_config(env)
    data = Path(env["HIDAGENT_DATA"])
    path = data / "worker.json"
    child = proxy = listener = None
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        for process in (proxy, child):
            if process is not None and process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with open(data / "worker.lock", "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def record(**extra):
            temporary = path.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as out:
                json.dump({"pid": os.getpid(), "instance": instance, **extra}, out)
            temporary.replace(path)

        record()
        try:
            if proxy_config and proxy_config["managed"]:
                # Owning the listener before probing prevents an unrelated 401
                # service from satisfying this worker's readiness check.
                family = socket.AF_INET6 if ":" in proxy_config["host"] else socket.AF_INET
                listener = socket.create_server((proxy_config["host"], proxy_config["port"]), family=family)
            if stopping:
                return 0
            child = subprocess.Popen([sys.executable, "-m", "hidagent"], cwd=ROOT / "server", env=env)
            if stopping:
                child.terminate()
            if not proxy_config or not proxy_config["managed"]:
                return child.wait()
            if stopping:
                return child.wait()
            descriptor = listener.fileno()
            try:
                proxy = subprocess.Popen([sys.executable, "-m", "hidagent.local_runtime", "clipboard-proxy",
                                          "--fd", str(descriptor),
                                          "--control-port", str(proxy_config["control_port"])],
                                         cwd=ROOT / "server", env=env, pass_fds=(descriptor,))
            finally:
                listener.close()
                listener = None
            deadline = time.monotonic() + 8
            while not stopping and time.monotonic() < deadline:
                if proxy.poll() is not None or child.poll() is not None:
                    break
                if _proxy_accepting(proxy_config) and proxy.poll() is None:
                    record(clipboard_proxy_ready=True, clipboard_proxy_pid=proxy.pid)
                    break
                time.sleep(.1)
            else:
                if not stopping:
                    raise RuntimeError("剪贴板代理启动超时；请检查 worker.log 和显式专用端口。")
            while not stopping:
                if proxy.poll() is not None:
                    raise RuntimeError("剪贴板代理已退出；请检查 worker.log 中的端口占用或权限错误。")
                try:
                    return child.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
            return child.wait(timeout=5)
        finally:
            if listener is not None:
                listener.close()
            for process in (proxy, child):
                if process is not None and process.poll() is None:
                    process.terminate()
            for process in (proxy, child):
                if process is not None:
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            current = _worker_record(data)
            if current and current.get("pid") == os.getpid() and current.get("instance") == instance:
                path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机网页与独立 Mac 镜像接收器")
    parser.add_argument("action", nargs="?", default="run",
                        choices=("run", "start", "status", "restart-worker", "stop", "worker", "clipboard-proxy"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backend", choices=("board", "uxplay"))
    parser.add_argument("--instance", help=argparse.SUPPRESS)
    parser.add_argument("--fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--control-port", type=int, default=8890, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.action == "clipboard-proxy":
        if args.fd is None or args.fd < 0 or not 1 <= args.control_port <= 65535:
            parser.error("managed clipboard proxy requires a listener fd and valid control port")
        serve_clipboard_proxy(args.fd, args.control_port)
        return 0
    env = runtime_environment()
    if args.backend:
        env = runtime_environment({**env, "SCREEN_BACKEND": args.backend})
    if args.action not in {"stop", "status"} and env["HOST"] not in ("127.0.0.1", "localhost"):
        raise RuntimeError("本地运行管理的 HOST 只支持 127.0.0.1 或 localhost，暂不支持 IPv6 回环。")
    proxy_config = clipboard_proxy_config(env) if args.action not in {"stop", "status"} else None
    if args.dry_run:
        print(json.dumps({"action": args.action, "backend": env["SCREEN_BACKEND"],
                          "url": f"http://{env['HOST']}:{int(env['PORT'])}",
                          "data": env["HIDAGENT_DATA"], "receiver_socket": env.get("AIRPLAY_SOCKET"),
                          "uxplay_found": bool(shutil.which("uxplay", path=env.get("PATH"))),
                          "model_configured": bool(env.get("LLM_MODEL")),
                          "clipboard_proxy": {"configured": proxy_config is not None, **(proxy_config or {})}},
                         ensure_ascii=False))
        return 0
    if args.action == "status":
        print(json.dumps(status(env), ensure_ascii=False))
        return 0
    if args.action == "worker":
        if not args.instance:
            parser.error("managed worker requires an instance")
        return worker(env, args.instance)
    with management_lock(Path(env["HIDAGENT_DATA"])):
        if args.action == "stop":
            stop_worker(env)
            stop_receiver(env)
        else:
            record = _worker_record(Path(env["HIDAGENT_DATA"]))
            if worker_status(env) is not None and not _owned_worker(record):
                raise RuntimeError("端口上已有未由此脚本管理的服务，请先完成现有服务迁移。")
            if args.action == "run" and _owned_worker(record):
                raise RuntimeError("后台网页服务已运行；使用 status 查看或 restart-worker 重启。")
            if args.action == "restart-worker":
                stop_worker(env)
            ensure_receiver(env)
            if args.action != "run":
                start_worker(env)
    if args.action == "run":
        os.chdir(ROOT / "server")
        os.execve(sys.executable, [sys.executable, "-m", "hidagent"], env)
    print(json.dumps(status(env), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
