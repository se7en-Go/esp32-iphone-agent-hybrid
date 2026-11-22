"""HTTP client for the iPhone control server, shared by the CLI and optional MCP bridge."""

import logging
import math
import os

import httpx

log = logging.getLogger(__name__)


def _client_timeout() -> float:
    raw = os.environ.get("IPHONE_AGENT_TIMEOUT", "").strip()
    if not raw:
        return 60.0
    try:
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout must be a positive finite number")
        return value
    except ValueError:
        log.warning("invalid IPHONE_AGENT_TIMEOUT=%r; using default 60s", raw)
        return 60.0


class IPhoneClient:
    def __init__(self, url: str | None = None, *, transport=None):
        url = url or os.environ.get("IPHONE_AGENT_URL", "http://127.0.0.1:8890")
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.query or parsed.fragment:
            raise ValueError("server URL must be an HTTP(S) URL without a query or fragment")
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=_client_timeout(),
                                 transport=transport, trust_env=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def request(self, method: str, path: str, body: dict | None = None):
        response = self.http.request(method, path, json=body)
        if response.is_error:
            try:
                detail = response.json().get("detail", "request failed")
            except ValueError:
                detail = "request failed"
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")
        response.raise_for_status()
        return response

    def status(self) -> dict:
        return self.request("GET", "/api/status").json()

    def middle_click_home_status(self) -> dict:
        return self.request("GET", "/api/control/middle-click-home").json()

    def enable_middle_click_home(
        self, screen_id: str, *, include_diff: bool = True, ocr_diff: bool = False
    ) -> dict:
        return self.request(
            "POST",
            "/api/control/middle-click-home/enable",
            {"screen_id": screen_id, "include_diff": include_diff, "ocr_diff": ocr_diff},
        ).json()

    def revoke_middle_click_home(self, *, all_devices: bool = False) -> dict:
        return self.request(
            "POST",
            "/api/control/middle-click-home/revoke",
            {"all_devices": bool(all_devices)},
        ).json()

    def capabilities(self) -> dict:
        return self.request("GET", "/api/control/capabilities").json()

    def screenshot(self) -> bytes:
        return self.request("GET", "/api/frame.jpg").content

    def observe(self, *, for_click: bool = False) -> dict:
        return self.request("GET", "/api/control/observe" + ("?for_click=true" if for_click else "")).json()

    def find_text(self, text: str, screen_id: str, *, nth: int = 1,
                  exact: bool = False, region: list[float] | None = None) -> dict:
        return self.request("POST", "/api/control/find_text", {
            "screen_id": screen_id, "text": text, "nth": nth, "exact": exact,
            **({"region": region} if region is not None else {}),
        }).json()

    def control(self, name: str, screen_id: str, **arguments) -> dict:
        return self.request("POST", "/api/control/action",
                            {"action": name, "screen_id": screen_id, **arguments}).json()

    def batch(self, screen_id: str, steps: list[dict]) -> dict:
        return self.request("POST", "/api/control/batch", {"screen_id": screen_id, "steps": steps}).json()

    def click_observed(self, screen_id: str, pointer_x: float, pointer_y: float,
                       target_box: tuple[float, float, float, float], *, target_label: str = "",
                       duration: float | None = None,
                       caret_scope: tuple[float, float, float, float] | None = None) -> dict:
        return self.request("POST", "/api/control/host-click", {
            "screen_id": screen_id, "pointer_x": pointer_x, "pointer_y": pointer_y,
            "target_box": target_box, "target_label": target_label,
            **({"duration": duration} if duration is not None else {}),
            **({"caret_scope": caret_scope} if caret_scope is not None else {}),
        }).json()

    def clipboard_stage(self, stage: str, **fields) -> dict:
        if stage not in {"prepare", "paste", "confirm", "status", "cancel"}:
            raise ValueError("unknown clipboard stage")
        return self.request("POST", f"/api/control/clipboard/{stage}", fields).json()

    def layouts(self) -> dict:
        return self.request("GET", "/api/layouts").json()

    def layout_tap(self, layout: str, point: str, screen_id: str) -> dict:
        return self.request("POST", "/api/control/layout-tap",
                            {"layout": layout, "point": point, "screen_id": screen_id}).json()

    def skills(self) -> dict:
        return self.request("GET", "/api/skills").json()

    def skill(self, name: str) -> dict:
        return self.request("GET", f"/api/skills/{name}").json()

    def skill_prepare(self, skill: str = "") -> dict:
        return self.request("POST", "/api/skill/prepare-send", {"skill": skill}).json()

    def skill_check(self, observed_text: str = "", skill: str = "") -> dict:
        return self.request("POST", "/api/skill/check",
                            {"observed_text": observed_text, "skill": skill}).json()

    def skill_confirm(self, bubble_visible: bool, observed_text: str = "", skill: str = "") -> dict:
        return self.request("POST", "/api/skill/confirm-send",
                            {"bubble_visible": bubble_visible, "observed_text": observed_text,
                             "skill": skill}).json()

    def action(self, name: str, **arguments) -> dict:
        if name not in ("tap", "swipe", "scroll", "type", "key", "home", "back"):
            raise ValueError("unknown control action")
        return self.request("POST", f"/api/{name}", arguments or {}).json()

    def calibrate(self, x: float | None = None, y: float | None = None) -> dict:
        if x is None and y is None:
            return self.request("POST", "/api/calibrate/start", {}).json()
        if x is None or y is None:
            raise ValueError("provide both pointer coordinates")
        return self.request("POST", "/api/calibrate/finish", {"x": x, "y": y}).json()

    def delegate(self, goal: str) -> dict:
        return self.request("POST", "/api/agent", {"text": goal}).json()

    def task_status(self) -> dict:
        return self.status()["agent"]

    def stop(self) -> dict:
        return self.request("POST", "/api/agent/stop", {}).json()
