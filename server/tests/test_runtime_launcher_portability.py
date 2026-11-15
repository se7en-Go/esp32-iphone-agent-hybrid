"""Windows launcher help must remain available without the Unix runtime or a venv."""

import builtins
import os
import runpy
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_windows_help_does_not_import_unix_runtime_or_start_process(monkeypatch, capsys, flag):
    launcher = Path(__file__).resolve().parents[2] / "scripts/run_server.py"
    original_import = builtins.__import__

    def import_without_unix(name, *args, **kwargs):
        assert name not in {"fcntl", "hidagent.local_runtime", "hidagent.airplay_receiver"}
        return original_import(name, *args, **kwargs)

    def no_exec(*_args, **_kwargs):
        raise AssertionError("help must not start a service")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", [str(launcher), flag])
    monkeypatch.setattr(builtins, "__import__", import_without_unix)
    monkeypatch.setattr(os, "execve", no_exec)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(launcher), run_name="__main__")
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "Windows" in output and "前台" in output
