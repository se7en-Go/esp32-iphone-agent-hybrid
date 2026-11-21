"""Exercise the real preview handlers with delayed responses, without a browser or phone."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_preview_rejects_responses_from_before_session_invalidation(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the frontend response-order regression")
    script = Path(__file__).with_name("static_preview_races.cjs")
    diagnostics = tmp_path / "preview-harness.json"
    try:
        result = subprocess.run([node, str(script), str(diagnostics)],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as error:
        progress = diagnostics.read_text() if diagnostics.exists() else "Node did not reach harness startup"
        pytest.fail(f"preview harness exceeded 30s; progress={progress}; "
                    f"stdout={error.stdout!r}; stderr={error.stderr!r}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(diagnostics.read_text())["phase"] == "complete"
