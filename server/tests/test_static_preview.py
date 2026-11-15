"""Exercise the real preview handlers with delayed responses, without a browser or phone."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_preview_rejects_responses_from_before_session_invalidation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the frontend response-order regression")
    script = Path(__file__).with_name("static_preview_races.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
