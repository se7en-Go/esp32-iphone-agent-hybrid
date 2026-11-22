"""入口脚本能打印帮助，并且文档没有叫人去装 Linux 子系统。"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BANNED = ("请先装 WSL", "请先安装 WSL", "install WSL", "先装 WSL2", "Windows 用户请先装 WSL")


def test_pair_and_unpair_are_positional():
    path = ROOT / "scripts" / "hid_mode.py"
    spec = importlib.util.spec_from_file_location("hid_mode", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    paired = mod._parse(["pair"])
    assert paired.pair and paired.mode is None and not paired.unpair
    unpaired = mod._parse(["unpair"])
    assert unpaired.unpair and unpaired.mode is None and not unpaired.pair
    flagged = mod._parse(["--pair"])
    assert flagged.pair and flagged.mode is None
    both = mod._parse(["ble", "--pair"])
    assert both.mode == "ble" and both.pair


@pytest.mark.parametrize("action", ["usb", "ble", "auto", "pair", "unpair", "--pair", "--unpair"])
def test_hid_status_rejects_changes(action):
    path = ROOT / "scripts" / "hid_mode.py"
    spec = importlib.util.spec_from_file_location("hid_mode", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    status = mod._parse(["--status", "--port", "COM5"])
    assert status.status and status.mode is None and not status.pair and not status.unpair
    with pytest.raises(SystemExit) as exc:
        mod._parse([action, "--status"])
    assert exc.value.code == 2


def test_help_exits_cleanly():
    for script in ("scripts/setup.py", "scripts/flash.py", "scripts/wifi_config.py", "scripts/mcp_config.py",
                   "scripts/hid_mode.py"):
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        result = subprocess.run(
            [sys.executable, str(ROOT / script), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        assert result.returncode == 0, result.stderr


def test_docs_do_not_tell_windows_users_to_install_a_subsystem():
    roots = [
        ROOT / "docs", ROOT / "scripts", ROOT / "skills", ROOT / "prompts",
        ROOT / "README.md", ROOT / "README.en.md",
    ]
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in {".md", ".py", ".ps1", ".sh", ""}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for phrase in BANNED:
                assert phrase not in text, f"{path} contains {phrase}"


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flash_env_firmware_bin_missing_fails_instead_of_falling_back(tmp_path, monkeypatch):
    mod = _load_script("flash.py")
    monkeypatch.setenv("FIRMWARE_BIN", str(tmp_path / "does-not-exist.bin"))
    with pytest.raises(SystemExit) as exc:
        mod.find_bin("")
    assert exc.value.code != 0


def test_flash_build_only_windows_uses_execution_policy_bypass(monkeypatch):
    mod = _load_script("flash.py")
    seen = []
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.subprocess, "call", lambda cmd: seen.append(cmd) or 0)
    assert mod.build_only() == 0
    assert seen and "-ExecutionPolicy" in seen[0] and "Bypass" in seen[0]


@pytest.mark.parametrize("arg", ["stop", "status", "restart-worker"])
def test_run_server_windows_rejects_unix_management_subcommands(monkeypatch, arg):
    mod = _load_script("run_server.py")
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.sys, "argv", ["run_server.py", arg])
    monkeypatch.setattr(mod, "require_venv", lambda: pytest.fail("must not launch server on unsupported Windows arg"))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code != 0


def test_wifi_config_rejects_ap_combined_with_ssid():
    mod = _load_script("wifi_config.py")
    with pytest.raises(SystemExit) as exc:
        mod.main(["--ap", "--ssid", "MyWiFi"])
    assert exc.value.code != 0


def test_mcp_config_honors_iphone_agent_url(monkeypatch, capsys):
    mod = _load_script("mcp_config.py")
    monkeypatch.setenv("IPHONE_AGENT_URL", "http://127.0.0.1:9911")
    monkeypatch.setattr(mod.sys, "argv", ["mcp_config.py"])
    monkeypatch.setattr(mod, "require_venv", lambda: Path("/fake/python"))
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:9911" in out
    assert "8890" not in out
