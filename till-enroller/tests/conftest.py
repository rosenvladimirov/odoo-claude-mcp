"""Мокове за till-enroller: лицензионният сървър (LS), Cloudflare (CF) и cert-issuer (CI).

Истински HTTP сървъри на 127.0.0.1 — клиентите на услугата се пробват такива, каквито са.
Всеки мок пази какво му е поискано и може да „падне“ на избрана стъпка.
"""

import base64
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import enroller as m  # noqa: E402

VECTORS = json.loads((Path(__file__).parent / "vectors_v1.json").read_text())

CF_TOKEN = "cf-api-token-SECRET-0123456789"
CF_ACC = "acc0000000000000000000000000000a"
CF_ZONE = "zone000000000000000000000000000z"
LS_KEY = "ls-api-key-SECRET-abcdef0123"
CI_ID = "ci-client-id-SECRET-1111.access"
CI_SECRET = "ci-client-secret-SECRET-2222"


class Mock:
    """Основа: HTTP сървър, дневник на заявките, инжектирани провали {(метод, ключ): статус}."""

    def __init__(self):
        self.requests = []
        self.fail = {}
        self.lock = threading.Lock()
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                u = urlparse(self.path)
                with mock.lock:
                    mock.requests.append({"method": self.command, "path": u.path, "query": parse_qs(u.query),
                                          "headers": dict(self.headers), "body": body})
                    status, out = mock.route(self.command, u.path, parse_qs(u.query), self.headers, body)
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _go

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def failing(self, method, key):
        return self.fail.get((method, key))

    def stop(self):
        self.srv.shutdown()


class MockCF(Mock):
    def __init__(self):
        self.tunnels, self.routes, self.dns = {}, {}, {}
        self.lost_create = False        # тунелът се създава, но отговорът е 500
        super().__init__()

    @staticmethod
    def ok(result):
        return 200, {"success": True, "errors": [], "result": result}

    def route(self, method, path, q, headers, body):
        if headers.get("Authorization") != "Bearer " + CF_TOKEN:
            return 403, {"success": False, "errors": [{"code": 10000, "message": "auth"}]}
        acc, zone = f"/client/v4/accounts/{CF_ACC}", f"/client/v4/zones/{CF_ZONE}"
        if path.startswith(acc + "/cfd_tunnel"):
            rest = path[len(acc + "/cfd_tunnel"):]
            if method == "GET" and rest == "":
                name = q.get("name", [None])[0]
                return self.ok([t for t in self.tunnels.values() if name in (None, t["name"])])
            if method == "POST" and rest == "":
                if st := self.failing("POST", "tunnel"):
                    return st, {"success": False, "errors": [{"code": 1}]}
                t = {"id": str(uuid.uuid4()), "name": body["name"]}
                self.tunnels[t["id"]] = t
                if self.lost_create:
                    return 500, {"success": False, "errors": [{"code": 2}]}
                return self.ok(t)
            tid = rest.strip("/").split("/")[0]
            if tid not in self.tunnels:
                return 404, {"success": False, "errors": [{"code": 404}]}
            if method == "GET" and rest.endswith("/token"):
                if st := self.failing("GET", "token"):
                    return st, {"success": False, "errors": [{"code": 3}]}
                return self.ok(base64.b64encode(f"TUNNEL-TOKEN-SECRET-{tid}".encode()).decode())
            if method == "DELETE" and rest.endswith("/connections"):
                return self.ok(None)
            if method == "DELETE":
                if st := self.failing("DELETE", "tunnel"):
                    return st, {"success": False, "errors": [{"code": 4}]}
                del self.tunnels[tid]
                return self.ok({"id": tid})
        if path.startswith(acc + "/zerotrust/routes/hostname"):
            rest = path[len(acc + "/zerotrust/routes/hostname"):]
            if method == "GET":
                return self.ok(list(self.routes.values()))
            if method == "POST":
                if st := self.failing("POST", "route"):
                    return st, {"success": False, "errors": [{"code": 5}]}
                if any(r["hostname"] == body["hostname"] for r in self.routes.values()):
                    return 409, {"success": False, "errors": [{"code": 409}]}
                r = {"id": str(uuid.uuid4()), "hostname": body["hostname"], "tunnel_id": body["tunnel_id"]}
                self.routes[r["id"]] = r
                return self.ok(r)
            if method == "DELETE":
                if st := self.failing("DELETE", "route"):
                    return st, {"success": False, "errors": [{"code": 6}]}
                return self.ok(self.routes.pop(rest.strip("/"), None))
        if path.startswith(zone + "/dns_records"):
            rest = path[len(zone + "/dns_records"):].strip("/")
            if method == "GET":
                return self.ok([r for r in self.dns.values()
                                if r["name"] == q.get("name", [None])[0] and r["type"] == q.get("type", ["A"])[0]])
            if st := self.failing(method, "dns"):
                return st, {"success": False, "errors": [{"code": 7}]}
            if method == "POST":
                r = {"id": str(uuid.uuid4()), **body}
                self.dns[r["id"]] = r
                return self.ok(r)
            if method == "PUT" and rest in self.dns:
                self.dns[rest] = {"id": rest, **body}
                return self.ok(self.dns[rest])
            if method == "DELETE" and rest in self.dns:
                return self.ok(self.dns.pop(rest))
        return 404, {"success": False, "errors": [{"code": 404, "message": path}]}

    def dns_writes(self):
        return [r for r in self.requests if "/dns_records" in r["path"] and r["method"] != "GET"]


class MockCI(Mock):
    def __init__(self):
        self.tills = {}
        super().__init__()

    def route(self, method, path, q, headers, body):
        # Ръбът на Access: без service token-а → 403 (там) — тук е 401 като в cert-issuer.
        if headers.get("CF-Access-Client-Id") != CI_ID or headers.get("CF-Access-Client-Secret") != CI_SECRET:
            return 401, {"detail": "admin: Cloudflare Access service token required"}
        if st := self.failing(method, path.rsplit("/", 1)[-1] if path.endswith("/san") else "tills"):
            return st, {"detail": "boom"}
        if method == "POST" and path == "/v1/admin/tills":
            self.tills[body["till"]] = {"extra": []}
            return 201, {"till": body["till"], "secret": "CI-TILL-SECRET-" + body["till"]}
        if method == "DELETE" and path.startswith("/v1/admin/tills/"):
            host = path.split("/")[-1]
            return 200, {"till": host, "removed": self.tills.pop(host, None) is not None}
        if method == "POST" and path.endswith("/san"):
            host = path.split("/")[-2]
            if host not in self.tills:
                return 404, {"detail": "unknown machine"}
            till, client = host.split(".")[:2]
            name = f"{till}.{client}.lan.mcpworks.net"
            self.tills[host]["extra"] = sorted(set(self.tills[host]["extra"]) | {name})
            return 200, {"till": host, "extra": self.tills[host]["extra"]}
        return 404, {"detail": "Not Found"}


class MockLS(Mock):
    """`/l10n_bg/erpnet/enroller/*`; разрешението се подписва с тестовия ключ от векторите."""

    def __init__(self):
        self.queue = []
        self.reports, self.stored, self.done = [], {}, []
        self.grant_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(VECTORS["grant_private_key_b64"]))
        super().__init__()

    def grant_token(self, till_ref, tunnel_id, host):
        payload = dict(VECTORS["grant"]["payload"], till_ref=till_ref, cf_tunnel_id=tunnel_id, cf_hostname=host)
        sig = base64.b64encode(self.grant_key.sign(m.canonical_json(payload).encode())).decode()
        return m.b64u(m.canonical_json({"payload": payload, "sig": sig}).encode())

    def route(self, method, path, q, headers, body):
        if headers.get("Authorization") != "Bearer " + LS_KEY:
            return 401, {"error": "bad_api_key"}
        r = path.rsplit("/", 1)[-1]
        if st := self.failing("POST", r):
            return st, {"error": "boom"}
        if r == "pull":
            return 200, {"tills": list(self.queue)}
        if r == "report":
            self.reports.append(body)
            return 200, {"grant_token": self.grant_token(body["till_ref"], body["cf_tunnel_id"], body["cf_hostname"]),
                         "reg": "https://zzclient.example.com"}
        if r == "store":
            self.stored[body["till_ref"]] = body["sealed"]
            return 200, {"state": "issued"}
        if r == "done":
            self.done.append(body)
            # затворено действие изчезва от опашката
            self.queue = [t for t in self.queue if not (t["till_ref"] == body["till_ref"] and t["action"] == body["action"]
                                                       and body["ok"])]
            return 200, {"ok": True}
        return 404, {"error": "not_found"}


ENV = {"CF_API_TOKEN": CF_TOKEN, "CF_ACCOUNT_ID": CF_ACC, "CF_ZONE_ID": CF_ZONE, "LS_API_KEY": LS_KEY,
       "CI_ACCESS_CLIENT_ID": CI_ID, "CI_ACCESS_CLIENT_SECRET": CI_SECRET,
       "CI_PULL_URL": "https://certs.mcpworks.net"}


@pytest.fixture
def world():
    ls, cf, ci = MockLS(), MockCF(), MockCI()
    env = dict(ENV, LS_URL=ls.url, CI_URL=ci.url)
    enr = m.build(env, cf_api=cf.url + "/client/v4")
    yield type("World", (), {"ls": ls, "cf": cf, "ci": ci, "enr": enr, "env": env})
    for x in (ls, cf, ci):
        x.stop()


def item(action="create", ref="6f1d2c3b-aaaa-4bbb-8ccc-0123456789ab", client="zzclient", till="kasa1", **kw):
    return {"till_ref": ref, "client_slug": client, "till": till, "box_pub": VECTORS["box_pub_b64u"],
            "action": action, **kw}
