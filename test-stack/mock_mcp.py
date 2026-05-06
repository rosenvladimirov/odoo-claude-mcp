#!/usr/bin/env python3
"""Minimal MCP stub for gateway testing — only the endpoints the gateway calls."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import hashlib


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        # Quieter logs.
        print(f"[mock-mcp] {self.command} {self.path} {fmt % a}", flush=True)

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send_json(200, {"status": "ok", "mock": True})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/user/register-connection":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except Exception:
                return self._send_json(400, {"error": "invalid json"})

            api_key = body.get("api_key", "")
            url = body.get("url", "")
            db = body.get("db", "")
            login = body.get("login", "")

            # Reject obviously invalid keys to test failure paths.
            if not api_key or api_key == "INVALID":
                return self._send_json(401, {"error": "invalid api key"})

            # Deterministic profile hash from (url, db, login).
            profile = hashlib.sha256(
                f"{url}|{db}|{login}".encode()
            ).hexdigest()[:32]

            return self._send_json(200, {
                "profile": profile,
                "owner": profile,
                "name": body.get("name", "User"),
                "alias": body.get("alias", "default"),
            })
        return self._send_json(404, {"error": "not found"})


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8084), MockHandler)
    print("[mock-mcp] listening on :8084", flush=True)
    server.serve_forever()
