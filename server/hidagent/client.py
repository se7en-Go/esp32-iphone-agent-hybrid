"""HTTP client for the iPhone control server, shared by the CLI and optional MCP bridge."""

import os

import httpx


class IPhoneClient:
    def __init__(self, url: str | None = None, *, transport=None):
        url = url or os.environ.get("IPHONE_AGENT_URL", "http://127.0.0.1:8890")
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.query or parsed.fragment:
            raise ValueError("server URL must be an HTTP(S) URL without a query or fragment")
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=60, transport=transport, trust_env=False)

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

    def screenshot(self) -> bytes:
        return self.request("GET", "/api/frame.jpg").content

    def observe(self) -> dict:
        return self.request("GET", "/api/control/observe").json()

    def control(self, name: str, screen_id: str, **arguments) -> dict:
        return self.request("POST", "/api/control/action",
                            {"action": name, "screen_id": screen_id, **arguments}).json()

    def batch(self, screen_id: str, steps: list[dict]) -> dict:
        return self.request("POST", "/api/control/batch", {"screen_id": screen_id, "steps": steps}).json()

    def layouts(self) -> dict:
        return self.request("GET", "/api/layouts").json()

    def layout_tap(self, layout: str, point: str, screen_id: str) -> dict:
        return self.request("POST", "/api/control/layout-tap",
                            {"layout": layout, "point": point, "screen_id": screen_id}).json()

    def skills(self) -> dict:
        return self.request("GET", "/api/skills").json()

    def skill(self, name: str) -> dict:
        return self.request("GET", f"/api/skills/{name}").json()

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
        return self.request("POST", f"/api/{name}", arguments or None).json()

    def calibrate(self, x: float | None = None, y: float | None = None) -> dict:
        if x is None and y is None:
            return self.request("POST", "/api/calibrate/start").json()
        if x is None or y is None:
            raise ValueError("provide both pointer coordinates")
        return self.request("POST", "/api/calibrate/finish", {"x": x, "y": y}).json()

    def delegate(self, goal: str) -> dict:
        return self.request("POST", "/api/agent", {"text": goal}).json()

    def task_status(self) -> dict:
        return self.status()["agent"]

    def stop(self) -> dict:
        return self.request("POST", "/api/agent/stop").json()
