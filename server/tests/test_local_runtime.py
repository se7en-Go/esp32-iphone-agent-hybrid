"""Startup can be inspected without spawning hardware processes or requiring a model key."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Skip only Unix process-manager tests, not the cross-platform launcher or board tests.
pytest.importorskip("fcntl", reason="background receiver process management is Unix-only")

from hidagent import local_runtime


class BoundListener:
    family = socket.AF_INET

    def __init__(self):
        self.closed = False

    def fileno(self):
        return 71

    def getsockname(self):
        return "127.0.0.1", 8891

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@pytest.fixture(autouse=True)
def bound_listeners(monkeypatch):
    """Management tests use fake binds; the explicit subprocess test uses port zero."""
    listeners = []

    def bind(*_args, **_kwargs):
        listener = BoundListener()
        listeners.append(listener)
        return listener

    monkeypatch.setattr(socket, "create_server", bind)
    return listeners


def test_discovers_local_receiver_and_preserves_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.sys, "platform", "darwin")
    bundle = tmp_path / "server/.local-data/uxplay-runtime"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin/uxplay").touch()
    data = tmp_path / "local"
    data.mkdir()
    (data / "runtime.env").write_text("AIRPLAY_NAME=Persistent receiver\nLLM_MODEL=optional\n")
    (data / "uxplay.conf").touch()
    env = local_runtime.runtime_environment({"HIDAGENT_DATA": str(data), "PATH": "/bin", "LLM_MODEL": "chosen"})
    assert env["SCREEN_BACKEND"] == "uxplay"
    assert env["PATH"].startswith(str(bundle / "bin"))
    assert env["AIRPLAY_NAME"] == "Persistent receiver"
    assert env["AIRPLAY_SOCKET"] == str(data / "airplay.sock")
    assert env["UXPLAYRC"] == str(data / "uxplay.conf")
    assert env["LLM_MODEL"] == "chosen"


def test_model_is_optional_and_dry_run_has_no_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.os, "environ", {"PATH": "/bin"})
    monkeypatch.setattr(local_runtime, "_start_process", lambda *_: (_ for _ in ()).throw(AssertionError("spawn")))
    assert local_runtime.main(["start", "--dry-run", "--backend", "board"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["model_configured"] is False
    assert output["backend"] == "board"
    assert not (tmp_path / "server/.local-data").exists()


@pytest.mark.parametrize("action", ["run", "start", "restart-worker", "worker"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_ipv6_control_host_is_rejected_before_runtime_mutation(tmp_path, monkeypatch, action, dry_run):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.os, "environ", {"HOST": "::1", "SCREEN_BACKEND": "board", "PATH": "/bin"})
    for name in ("ensure_receiver", "start_worker", "stop_worker", "worker", "worker_status"):
        monkeypatch.setattr(local_runtime, name, lambda *_: pytest.fail("runtime touched before host validation"))
    arguments = [action, "--instance", "a" * 32] + (["--dry-run"] if dry_run else [])
    with pytest.raises(RuntimeError, match="127.0.0.1.*localhost"):
        local_runtime.main(arguments)
    assert not (tmp_path / "server/.local-data").exists()


def test_restart_does_not_stop_or_replace_receiver(tmp_path, monkeypatch):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.os, "environ", {"SCREEN_BACKEND": "uxplay", "PATH": "/bin"})
    calls = []
    monkeypatch.setattr(local_runtime, "stop_worker", lambda _: calls.append("stop_worker"))
    monkeypatch.setattr(local_runtime, "ensure_receiver", lambda _: calls.append("reuse_receiver"))
    monkeypatch.setattr(local_runtime, "start_worker", lambda _: calls.append("start_worker"))
    monkeypatch.setattr(local_runtime, "status", lambda _: {})
    monkeypatch.setattr(local_runtime, "worker_status", lambda _: None)
    assert local_runtime.main(["restart-worker"]) == 0
    assert calls == ["stop_worker", "reuse_receiver", "start_worker"]


def test_pid_reuse_does_not_signal_an_unrelated_process(tmp_path, monkeypatch):
    (tmp_path / "worker.json").write_text(json.dumps({"pid": 123, "instance": "a" * 32}))
    monkeypatch.setattr(local_runtime.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 0, stdout="/usr/bin/some_other_program", stderr=""))
    monkeypatch.setattr(local_runtime.os, "kill", lambda *_: (_ for _ in ()).throw(AssertionError("signalled")))
    local_runtime.stop_worker({"HIDAGENT_DATA": str(tmp_path)})


def test_unmanaged_worker_blocks_receiver_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.os, "environ", {"SCREEN_BACKEND": "uxplay", "PATH": "/bin"})
    monkeypatch.setattr(local_runtime, "worker_status", lambda _: {"ready": True})
    monkeypatch.setattr(local_runtime, "ensure_receiver", lambda _: (_ for _ in ()).throw(AssertionError("receiver")))
    with pytest.raises(RuntimeError, match="迁移"):
        local_runtime.main(["start"])


def test_launcher_dry_run_uses_repository_module(tmp_path):
    root = Path(__file__).resolve().parents[2]
    # CI installs into setup-python, not server/.venv. Supply only that
    # environment dependency; retain the real launcher chdir/exec/module path.
    bootstrap = """
import runpy, sys
from pathlib import Path
launcher = Path(sys.argv[1])
sys.path.insert(0, str(launcher.parent))
import runtime
runtime.require_venv = lambda: Path(sys.executable)
sys.argv = sys.argv[1:]
runpy.run_path(str(launcher), run_name="__main__")
"""
    result = subprocess.run([sys.executable, "-c", bootstrap, str(root / "scripts/run_server.py"),
                             "start", "--dry-run"],
                            cwd=tmp_path, env={**os.environ, "SCREEN_BACKEND": "board",
                                              "HIDAGENT_DATA": str(tmp_path / "data")},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["action"] == "start"


def clipboard_env(data, **changes):
    return {"HIDAGENT_DATA": str(data), "HOST": "127.0.0.1", "PORT": "8890", "SCREEN_BACKEND": "board",
            "HIDAGENT_CLIPBOARD_BASE_URL": "http://computer.local:8891", "HIDAGENT_CLIPBOARD_KEY": "x" * 40,
            **changes}


def test_clipboard_proxy_config_uses_advertised_port_without_exposing_key(tmp_path):
    config = local_runtime.clipboard_proxy_config(clipboard_env(tmp_path))
    assert config == {"managed": True, "host": "0.0.0.0", "port": 8891, "control_port": 8890}
    assert "x" * 40 not in json.dumps(config)
    assert local_runtime.clipboard_proxy_config(clipboard_env(
        tmp_path, HIDAGENT_CLIPBOARD_BASE_URL="http://computer.local"))["port"] == 80
    assert local_runtime.clipboard_proxy_config({"PORT": "8890"}) is None
    assert local_runtime.clipboard_proxy_config(clipboard_env(
        tmp_path, HIDAGENT_CLIPBOARD_BASE_URL="https://computer.local", HIDAGENT_CLIPBOARD_MANAGED_PROXY="0"
    )) == {"managed": False}


@pytest.mark.parametrize("changes", [
    {"HIDAGENT_CLIPBOARD_KEY": ""}, {"HIDAGENT_CLIPBOARD_BASE_URL": ""},
    {"HIDAGENT_CLIPBOARD_BASE_URL": "https://computer.local:8891"},
    {"HIDAGENT_CLIPBOARD_BASE_URL": "http://user:secret@computer.local:8891"},
    {"HIDAGENT_CLIPBOARD_BASE_URL": "http://computer.local:8891?query=secret"},
    {"HIDAGENT_CLIPBOARD_BASE_URL": "http://computer.local:8891#fragment"},
    {"HIDAGENT_CLIPBOARD_BASE_URL": "http://computer.local:8891/prefix"},
    {"HIDAGENT_CLIPBOARD_BASE_URL": "http://computer.local:8890"},
    {"HIDAGENT_CLIPBOARD_BASE_URL": "http://computer.local:70000"},
    {"HIDAGENT_CLIPBOARD_MANAGED_PROXY": "invalid"},
])
def test_bad_clipboard_config_fails_before_any_service_start(tmp_path, monkeypatch, changes):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.os, "environ", clipboard_env(tmp_path / "local", **changes))
    monkeypatch.setattr(local_runtime, "ensure_receiver", lambda *_: pytest.fail("started receiver"))
    monkeypatch.setattr(local_runtime, "start_worker", lambda *_: pytest.fail("started worker"))
    with pytest.raises((RuntimeError, ValueError)) as error:
        local_runtime.main(["start"])
    assert "x" * 40 not in str(error.value) and "user:secret" not in str(error.value)
    assert not (tmp_path / "local").exists()


class ManagedChild:
    def __init__(self, pid, *, outcome=None):
        self.pid, self.returncode, self.outcome = pid, None, outcome
        self.terminated, self.killed = 0, 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        if self.outcome is not None:
            outcome, self.outcome = self.outcome, None
            outcome()
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake managed child", timeout)
        return self.returncode


@pytest.mark.parametrize("event", ["api_exit", "proxy_exit", "stop"])
def test_managed_worker_owns_proxy_and_cleans_both_children(tmp_path, monkeypatch, event, bound_listeners):
    calls, handlers = [], {}
    api, proxy = ManagedChild(101), ManagedChild(102)
    monkeypatch.setattr(local_runtime.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))
    monkeypatch.setattr(local_runtime, "_proxy_accepting", lambda _config: True)

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return api if len(calls) == 1 else proxy

    def change():
        record = json.loads((tmp_path / "worker.json").read_text())
        assert record["clipboard_proxy_ready"] is True
        if event == "api_exit":
            api.returncode = 0
        elif event == "proxy_exit":
            proxy.returncode = 3
        else:
            handlers[local_runtime.signal.SIGTERM](None, None)

    api.outcome = change
    monkeypatch.setattr(local_runtime.subprocess, "Popen", spawn)
    if event == "proxy_exit":
        with pytest.raises(RuntimeError, match="剪贴板"):
            local_runtime.worker(clipboard_env(tmp_path), "a" * 32)
    else:
        local_runtime.worker(clipboard_env(tmp_path), "a" * 32)
    assert len(calls) == 2 and calls[1][0][-5:] == ["clipboard-proxy", "--fd", "71", "--control-port", "8890"]
    assert calls[1][1]["pass_fds"] == (71,)
    assert len(bound_listeners) == 1 and bound_listeners[0].closed
    assert "x" * 40 not in " ".join(calls[1][0])
    assert api.poll() is not None and proxy.poll() is not None
    assert not (tmp_path / "worker.json").exists()


@pytest.mark.parametrize("configured", [False, True])
def test_unconfigured_or_external_proxy_keeps_single_worker(tmp_path, monkeypatch, configured, bound_listeners):
    calls = []
    child = ManagedChild(101, outcome=lambda: setattr(child, "returncode", 0))
    monkeypatch.setattr(local_runtime.signal, "signal", lambda *_: None)
    monkeypatch.setattr(local_runtime.subprocess, "Popen", lambda command, **kwargs: calls.append(command) or child)
    env = clipboard_env(tmp_path, HIDAGENT_CLIPBOARD_MANAGED_PROXY="0") if configured else {
        "HIDAGENT_DATA": str(tmp_path), "PORT": "8890"}
    assert local_runtime.worker(env, "a" * 32) == 0
    assert calls == [[sys.executable, "-m", "hidagent"]]
    assert not bound_listeners
    assert not (tmp_path / "worker.json").exists()


@pytest.mark.parametrize("failure", ["spawn", "bind", "timeout"])
def test_proxy_start_failure_always_cleans_api_and_record(tmp_path, monkeypatch, failure, bound_listeners):
    api, proxy = ManagedChild(101), ManagedChild(102)
    spawned = []
    monkeypatch.setattr(local_runtime.signal, "signal", lambda *_: None)
    monkeypatch.setattr(local_runtime, "_proxy_accepting", lambda _: False)
    ticks = iter([0., 9.])
    if failure == "timeout":
        monkeypatch.setattr(local_runtime.time, "monotonic", lambda: next(ticks))

    def spawn(command, **kwargs):
        spawned.append(command)
        if len(spawned) == 1:
            return api
        if failure == "spawn":
            raise OSError("simulated spawn failure")
        if failure == "bind":
            proxy.returncode = 1
        return proxy

    monkeypatch.setattr(local_runtime.subprocess, "Popen", spawn)
    with pytest.raises((OSError, RuntimeError)):
        local_runtime.worker(clipboard_env(tmp_path), "a" * 32)
    assert api.poll() is not None
    assert failure == "spawn" or proxy.poll() is not None
    assert len(bound_listeners) == 1 and bound_listeners[0].closed
    assert not (tmp_path / "worker.json").exists()


def test_restart_and_stop_leave_no_proxy_children(tmp_path, monkeypatch):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.os, "environ", clipboard_env(tmp_path))
    handlers, children = {}, []
    monkeypatch.setattr(local_runtime.signal, "signal", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(local_runtime, "_proxy_accepting", lambda _: True)
    monkeypatch.setattr(local_runtime, "worker_status", lambda _: None)
    monkeypatch.setattr(local_runtime, "status", lambda _: {})
    monkeypatch.setattr(local_runtime, "_owned_worker", lambda _record: False)
    monkeypatch.setattr(local_runtime, "ensure_receiver", lambda _: None)
    monkeypatch.setattr(local_runtime, "stop_receiver", lambda _: None)
    monkeypatch.setattr(local_runtime, "stop_worker", lambda _: None)

    def spawn(command, **kwargs):
        child = ManagedChild(101 + len(children))
        if command[-1] == "hidagent":
            child.outcome = lambda: handlers[local_runtime.signal.SIGTERM](None, None)
        children.append(child)
        return child

    monkeypatch.setattr(local_runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(local_runtime, "start_worker", lambda env: local_runtime.worker(env, "a" * 32))
    assert local_runtime.main(["restart-worker"]) == 0
    assert len(children) == 2 and all(child.poll() is not None for child in children)
    assert local_runtime.main(["restart-worker"]) == 0
    assert len(children) == 4 and all(child.poll() is not None for child in children)
    assert local_runtime.main(["stop"]) == 0
    assert not (tmp_path / "worker.json").exists()


def test_dry_run_reports_proxy_settings_but_not_pairing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(local_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(local_runtime.os, "environ", clipboard_env(tmp_path))
    monkeypatch.setattr(local_runtime.subprocess, "Popen", lambda *_args, **_kw: pytest.fail("spawn"))
    assert local_runtime.main(["start", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "x" * 40 not in output and "computer.local" not in output
    assert json.loads(output)["clipboard_proxy"]["port"] == 8891


def test_proxy_ignoring_term_is_reaped_during_worker_exit(tmp_path, monkeypatch):
    api, proxy = ManagedChild(101), ManagedChild(102)
    spawned = []
    monkeypatch.setattr(local_runtime.signal, "signal", lambda *_: None)
    monkeypatch.setattr(local_runtime, "_proxy_accepting", lambda _: True)
    proxy.terminate = lambda: setattr(proxy, "terminated", proxy.terminated + 1)
    api.outcome = lambda: setattr(api, "returncode", 0)
    monkeypatch.setattr(local_runtime.subprocess, "Popen",
                        lambda command, **kwargs: spawned.append(command) or (api if len(spawned) == 1 else proxy))
    assert local_runtime.worker(clipboard_env(tmp_path), "a" * 32) == 0
    assert proxy.terminated == 1 and proxy.killed == 1 and proxy.poll() == -9
    assert not (tmp_path / "worker.json").exists()


def test_existing_401_service_cannot_satisfy_new_proxy_readiness(tmp_path, monkeypatch):
    import errno

    spawned = []
    api, proxy = ManagedChild(101), ManagedChild(102)
    api.outcome = lambda: setattr(api, "returncode", 0)
    monkeypatch.setattr(local_runtime.signal, "signal", lambda *_: None)
    monkeypatch.setattr(local_runtime, "_proxy_accepting", lambda _: True)  # unrelated old proxy
    monkeypatch.setattr(local_runtime.subprocess, "Popen",
                        lambda command, **kwargs: spawned.append(command) or (api if len(spawned) == 1 else proxy))

    def occupied(*_args, **_kwargs):
        raise OSError(errno.EADDRINUSE, "simulated existing listener")

    monkeypatch.setattr(socket, "create_server", occupied)
    with pytest.raises(OSError, match="existing listener"):
        local_runtime.worker(clipboard_env(tmp_path), "a" * 32)
    assert not spawned
    assert not (tmp_path / "worker.json").exists()


def test_proxy_inherits_owned_socket_and_parent_closes_before_probe(tmp_path, monkeypatch, bound_listeners):
    spawned = []
    api, proxy = ManagedChild(101), ManagedChild(102)
    api.outcome = lambda: setattr(api, "returncode", 0)
    monkeypatch.setattr(local_runtime.signal, "signal", lambda *_: None)

    def spawn(command, **kwargs):
        spawned.append((command, kwargs))
        if len(spawned) == 2:
            assert len(bound_listeners) == 1 and not bound_listeners[0].closed
            assert kwargs["pass_fds"] == (71,)
            assert command[-4:] == ["--fd", "71", "--control-port", "8890"]
        return api if len(spawned) == 1 else proxy

    def ready(_config):
        assert bound_listeners and bound_listeners[0].closed
        return True

    monkeypatch.setattr(local_runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(local_runtime, "_proxy_accepting", ready)
    assert local_runtime.worker(clipboard_env(tmp_path), "a" * 32) == 0


def test_stop_during_listener_setup_closes_it_without_spawning(tmp_path, monkeypatch):
    handlers = {}
    listener = BoundListener()
    monkeypatch.setattr(local_runtime.signal, "signal", lambda sig, callback: handlers.__setitem__(sig, callback))

    def bind(*_args, **_kwargs):
        handlers[local_runtime.signal.SIGTERM](None, None)
        return listener

    monkeypatch.setattr(socket, "create_server", bind)
    monkeypatch.setattr(local_runtime.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("spawn after stop"))
    assert local_runtime.worker(clipboard_env(tmp_path), "a" * 32) == 0
    assert listener.closed
    assert not (tmp_path / "worker.json").exists()


@pytest.mark.parametrize("accepting", [True, False])
def test_proxy_child_serves_only_inherited_listener_and_closes_it(monkeypatch, accepting):
    import uvicorn

    listener, served = BoundListener(), []
    def option(level, name):
        # SO_ACCEPTCONN is unavailable on the user's Mac, despite the constant existing.
        if name == socket.SO_ACCEPTCONN:
            raise OSError(42, "Protocol not available")
        assert (level, name) == (socket.SOL_SOCKET, socket.SO_TYPE)
        return socket.SOCK_STREAM if accepting else socket.SOCK_DGRAM

    listener.getsockopt = option
    monkeypatch.setattr(socket, "socket", lambda *, fileno: listener if fileno == 71 else pytest.fail("wrong fd"))

    class Server:
        def __init__(self, config):
            assert config.access_log is False and config.proxy_headers is False

        def run(self, *, sockets):
            assert not listener.closed
            served.extend(sockets)

    monkeypatch.setattr(uvicorn, "Server", Server)
    if accepting:
        local_runtime.serve_clipboard_proxy(71, 8890)
        assert served == [listener]
    else:
        with pytest.raises(RuntimeError, match="listening socket"):
            local_runtime.serve_clipboard_proxy(71, 8890)
        assert not served
    assert listener.closed


def test_internal_proxy_dispatch_does_not_reload_environment_or_bind(monkeypatch):
    served = []
    monkeypatch.setattr(local_runtime, "runtime_environment", lambda: pytest.fail("reload environment"))
    monkeypatch.setattr(local_runtime, "serve_clipboard_proxy", lambda fd, port: served.append((fd, port)))
    assert local_runtime.main(["clipboard-proxy", "--fd", "71", "--control-port", "8890"]) == 0
    assert served == [(71, 8890)]


def test_real_proxy_subprocess_uses_inherited_ephemeral_listener(tmp_path):
    """Exercise the actual platform socket/uvicorn path, without devices or fixed ports."""
    class PrivateApi(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401 if self.path == "/api/clipboard/pending" else 404)
            self.end_headers()

        def log_message(self, *_args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), PrivateApi)
    thread = threading.Thread(target=lambda: backend.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    process = None
    log_path = tmp_path / "proxy.log"
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = listener.getsockname()
        descriptor = listener.fileno()
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "hidagent.local_runtime", "clipboard-proxy", "--fd", str(descriptor),
                 "--control-port", str(backend.server_port)], cwd=local_runtime.ROOT / "server",
                env={"PATH": os.defpath, "PYTHONUNBUFFERED": "1"}, pass_fds=(descriptor,),
                stdout=log, stderr=subprocess.STDOUT)
        listener.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None:
            if local_runtime._proxy_accepting({"host": address[0], "port": address[1]}):
                break
            time.sleep(.02)
        else:
            pytest.fail("inherited proxy failed to become ready: " + log_path.read_text())
        assert process.poll() is None
    finally:
        listener.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=2)
    # Closing the child releases the inherited listener; no orphan keeps its port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reclaimed:
        reclaimed.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reclaimed.bind(address)
