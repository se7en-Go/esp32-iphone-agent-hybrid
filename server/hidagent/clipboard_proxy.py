"""LAN listener for clipboard handoff only; the control API stays on loopback."""

import argparse
import re

import httpx
from fastapi import FastAPI, Request, Response

from .clipboard import NO_STORE

_TOKEN = r"[A-Za-z0-9_-]{43}"
_GET = re.compile(rf"/api/clipboard/(?:pending|current|{_TOKEN}(?:/status)?)\Z")
_POST = re.compile(rf"/api/clipboard/(?:resolve|{_TOKEN}/(?:ack|complete))\Z")


def create_app(*, control_port: int = 8890, transport=None) -> FastAPI:
    if not 1 <= control_port <= 65535:
        raise ValueError("control_port must be in 1..65535")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def forward(request: Request, path: str):
        route = "/" + path
        allowed = _GET if request.method == "GET" else _POST
        if request.url.query or not allowed.fullmatch(route):
            return Response(status_code=404, headers=NO_STORE)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return Response(status_code=413, headers=NO_STORE)
        headers = {name: request.headers[name] for name in ("authorization", "content-type")
                   if name in request.headers}
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{control_port}",
                                         transport=transport, trust_env=False, follow_redirects=False,
                                         timeout=5) as client:
                result = await client.request(request.method, route, content=bytes(body), headers=headers)
        except httpx.HTTPError:
            return Response("本机剪贴板服务暂不可用。", status_code=502, headers=NO_STORE)
        # Never forward redirects, cookies or server metadata from the private API.
        if 300 <= result.status_code < 400:
            return Response(status_code=502, headers=NO_STORE)
        returned_headers = {**NO_STORE}
        if "content-type" in result.headers:
            returned_headers["content-type"] = result.headers["content-type"]
        return Response(result.content, status_code=result.status_code, headers=returned_headers)

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="仅开放剪贴板交接；控制 API 保持本机监听。")
    parser.add_argument("--host", default="127.0.0.1", help="手机访问的监听地址，例如 0.0.0.0")
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--control-port", type=int, default=8890, help="本机控制 API 端口")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.port == args.control_port:
        parser.error("剪贴板端口须在 1..65535 之间，且不能与控制端口相同。")
    # One-time token paths are credentials. Do not include them in access logs.
    uvicorn.run(create_app(control_port=args.control_port), host=args.host, port=args.port,
                access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
