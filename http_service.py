#!/usr/bin/env python3
"""仅用标准库实现的可选 HTTP 中央规则服务。"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import json
from urllib.parse import parse_qs, urlparse

from rule_middleware import RuleMiddleware


class CentralRuleHttpService:
    def __init__(self, middleware: RuleMiddleware | None = None) -> None:
        self.middleware = middleware or RuleMiddleware()

    def handle_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.middleware.handle(payload)


def make_handler(service: CentralRuleHttpService):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in {"/events", "/event"}:
                self._send(404, {"ok": False, "status": "not_found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = service.handle_json(payload)
                self._send(200 if result.get("ok", True) else 422, result)
            except Exception as exc:  # JSON boundary returns structured error.
                self._send(400, {"ok": False, "status": "bad_request", "error": str(exc)})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in {"/inspect", "/health"}:
                self._send(404, {"ok": False, "status": "not_found"})
                return
            if parsed.path == "/health":
                self._send(200, {"ok": True, "status": "synthetic-central-online"})
                return
            query = parse_qs(parsed.query)
            at = int(query["at"][0]) if "at" in query else None
            if "promise" in query:
                view = service.middleware.inspect_promise(query["promise"][0], at)
            elif "request" in query:
                view = service.middleware.inspect_request(query["request"][0], at)
            else:
                view = {"ok": False, "status": "invalid_inspect"}
            self._send(200 if view.get("ok", True) else 404, view)

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep test/demo output clean; callers can still inspect results.
            return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    service = CentralRuleHttpService()
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"synthetic rule center listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.host, args.port)
