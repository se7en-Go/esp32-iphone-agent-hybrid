from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hidagent import app
from hidagent.screen_uxplay import UxPlayScreen


def test_status_names_the_mac_receiver_when_using_uxplay(monkeypatch):
    monkeypatch.setattr(app, "screen", UxPlayScreen("ESP32 Agent Mac"))
    status = app.status()

    assert status["link"]["receiver"] == "ESP32 Agent Mac"
    assert "「ESP32 Agent Mac」" in status["hints"]["screen"]["zh"]


def test_replay_is_unavailable_even_while_the_transport_remains_live(monkeypatch):
    preview = SimpleNamespace(
        state="replaying", frame=None, frame_age_s=0.2, replay_lag_s=175.5, heartbeat_age_s=0.1, generation=2
    )
    screen = SimpleNamespace(live=True, frame=b"old", aspect=0.45, preview=lambda: preview)
    board = {"ap": "streaming", "fps": 4.5, "vage": 83, "fz": 1, "vtok": "do-not-expose"}
    monkeypatch.setattr(app, "screen", screen)
    monkeypatch.setattr(app, "device", SimpleNamespace(connected=True, board_status=board))
    status = app.status()

    assert status["screen"] is False
    assert status["ready"] is False
    assert status["task_ready"] is False
    assert status["video"]["state"] == "replaying"
    assert status["video"]["replay_lag_s"] == 175.5
    assert status["video"]["board"] == {"ap": "streaming", "fps": 4.5, "vage": 83, "fz": 1}
    with pytest.raises(HTTPException) as caught:
        app.frame()
    assert caught.value.status_code == 404
    assert caught.value.headers["X-Preview-State"] == "replaying"


def test_preview_discloses_age_of_retained_still_frame(monkeypatch):
    preview = SimpleNamespace(state="idle", frame=b"recent image", frame_age_s=7.2,
                              replay_lag_s=0.1, heartbeat_age_s=0.1, generation=1)
    monkeypatch.setattr(app, "screen", SimpleNamespace(live=True, frame=b"other", preview=lambda: preview))

    response = app.frame()
    assert response.body == b"recent image"
    assert response.headers["X-Preview-State"] == "idle"
    assert response.headers["X-Frame-Age"] == "7.2"
    assert response.headers["X-Preview-Generation"] == "1"
    assert response.headers["Cache-Control"] == "no-store"


def test_healthy_still_session_can_try_to_refresh_before_task(monkeypatch):
    preview = SimpleNamespace(state="waiting", frame=None, frame_age_s=20.0,
                              replay_lag_s=0.1, heartbeat_age_s=0.1, generation=1)
    monkeypatch.setattr(app, "screen", SimpleNamespace(live=True, aspect=0.45, preview=lambda: preview))
    monkeypatch.setattr(app, "phone", SimpleNamespace(calibrated=True))
    board = {"ap": "streaming", "fz": 0}
    monkeypatch.setattr(app, "device", SimpleNamespace(connected=True, board_status=board))

    status = app.status()
    assert status["screen"] is False
    assert status["task_ready"] is True

    board["fz"] = 1
    assert app.status()["task_ready"] is False
