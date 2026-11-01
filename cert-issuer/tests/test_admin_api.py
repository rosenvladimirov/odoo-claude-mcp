"""cert-issuer: admin API за till-enroller (ADR erpnet-kasi-fleet/0012, 0013).

Cloudflare Access е подменен: екипът е местен RSA ключ, а JWT-тата се секат тук."""

import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issuer as m  # noqa: E402
from test_issuer import FakeACME  # noqa: E402

TEAM = "zzteam.cloudflareaccess.com"
AUD = "aud-cert-issuer-admin"
CLIENT_ID = "enroller-client-id.access"
HOST = "kasa1.zzclient.erpnet.mcpworks.net"


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Team:
    """Ключовете на екипа в Access и секачът на JWT."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.fetches = 0

    def jwks(self):
        self.fetches += 1
        n = self.key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256",
                          "n": b64u(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
                          "e": b64u(n.e.to_bytes(3, "big"))}]}

    def jwt(self, key=None, kid="k1", alg="RS256", **claims):
        body = {"aud": [AUD], "iss": f"https://{TEAM}", "exp": int(time.time()) + 300,
                "iat": int(time.time()), "common_name": CLIENT_ID, "type": "app"}
        body.update(claims)
        body = {k: v for k, v in body.items() if v is not None}
        h = b64u(json.dumps({"alg": alg, "kid": kid}).encode())
        p = b64u(json.dumps(body).encode())
        sig = (key or self.key).sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{b64u(sig)}"


@pytest.fixture
def stack(tmp_path):
    team = Team()
    acme = FakeACME()
    iss = m.Issuer(tmp_path, acme)
    access = m.AccessVerifier(TEAM, AUD, (CLIENT_ID,), fetch_keys=team.jwks)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.make_handler(iss, access))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"

    def call(method, path, body=None, jwt=None, headers=None):
        hd = dict(headers or {})
        if jwt:
            hd["Cf-Access-Jwt-Assertion"] = jwt
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            hd["Content-Type"] = "application/json"
        req = urllib.request.Request(url + path, data=data, method=method, headers=hd)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    yield iss, team, call, tmp_path
    srv.shutdown()


def test_without_access_everything_is_401(stack):
    iss, team, call, _ = stack
    assert call("POST", "/v1/admin/tills", {"till": HOST})[0] == 401
    assert call("DELETE", f"/v1/admin/tills/{HOST}")[0] == 401
    assert call("POST", f"/v1/admin/tills/{HOST}/san", {})[0] == 401
    # подправени заглавки на Access/service token без подписан JWT не стигат
    forged = {"Cf-Access-Authenticated-User-Email": "rosen@example.com",
              "CF-Access-Client-Id": CLIENT_ID, "CF-Access-Client-Secret": "x"}
    assert call("POST", "/v1/admin/tills", {"till": HOST}, headers=forged)[0] == 401
    assert iss._load()["tills"] == {}


@pytest.mark.parametrize("bad", [
    {"key": "other"},                       # подписан с чужд ключ
    {"aud": ["other-app"]},
    {"iss": "https://evil.cloudflareaccess.com"},
    {"exp": int(time.time()) - 3600},
    {"common_name": "someone-else.access"},
    {"common_name": None, "email": "rosen@example.com"},   # човек, не service token
    {"alg": "HS256"},
    {"kid": "unknown"},
])
def test_a_bad_token_is_401(stack, bad):
    iss, team, call, _ = stack
    kw = dict(bad)
    if kw.pop("key", None):
        kw["key"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert call("POST", "/v1/admin/tills", {"till": HOST}, jwt=team.jwt(**kw))[0] == 401
    assert iss._load()["tills"] == {}


def test_not_configured_is_401(tmp_path):
    v = m.AccessVerifier("", "", ())
    assert not v.configured and v.verify(Team().jwt()) is None


def test_add_keeps_only_the_hash_and_the_till_pulls(stack):
    iss, team, call, tmp_path = stack
    st, body = call("POST", "/v1/admin/tills", {"till": HOST}, jwt=team.jwt())
    assert st == 201 and body["till"] == HOST and body["secret"]
    raw = (tmp_path / "registry.json").read_text()
    assert body["secret"] not in raw and m._sha256(body["secret"]) in raw
    assert iss.wanted_names() == ["*.erpnet.mcpworks.net", "*.zzclient.erpnet.mcpworks.net"]
    iss.ensure()
    st, cert = iss.for_till(body["secret"])
    assert st == 200 and "*.zzclient.erpnet.mcpworks.net" in cert["names"]


@pytest.mark.parametrize("host", [
    "kasa1.zzclient.example.com", "zzclient.erpnet.mcpworks.net", "a.kasa1.zzclient.erpnet.mcpworks.net",
    "Kasa1.zzclient.erpnet.mcpworks.net", "kasa_1.zzclient.erpnet.mcpworks.net", "zzclient/kasa1", "",
    "x" * 31 + ".zzclient.erpnet.mcpworks.net",
])
def test_bad_hostname_is_400(stack, host):
    iss, team, call, _ = stack
    assert call("POST", "/v1/admin/tills", {"till": host}, jwt=team.jwt())[0] == 400
    assert iss._load()["tills"] == {}


def test_lan_san_goes_only_to_its_machine(stack):
    iss, team, call, _ = stack
    s1 = call("POST", "/v1/admin/tills", {"till": HOST}, jwt=team.jwt())[1]["secret"]
    s2 = call("POST", "/v1/admin/tills", {"till": "kasa2.zzclient.erpnet.mcpworks.net"}, jwt=team.jwt())[1]["secret"]
    assert call("POST", "/v1/admin/tills/kasa9.zzclient.erpnet.mcpworks.net/san", {}, jwt=team.jwt())[0] == 404
    st, body = call("POST", f"/v1/admin/tills/{HOST}/san", {}, jwt=team.jwt())
    assert st == 200 and body["extra"] == ["kasa1.zzclient.lan.mcpworks.net"]
    # повторът не дублира
    assert call("POST", f"/v1/admin/tills/{HOST}/san", {}, jwt=team.jwt())[1]["extra"] == body["extra"]
    iss.ensure_all()
    a, b = iss.for_till(s1)[1], iss.for_till(s2)[1]
    leaf = x509.load_pem_x509_certificate(a["extra"]["chain"].encode())
    assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
        .get_values_for_type(x509.DNSName) == ["kasa1.zzclient.lan.mcpworks.net"]
    assert "extra" not in b


def test_delete_stops_the_till_and_is_idempotent(stack):
    iss, team, call, tmp_path = stack
    s1 = call("POST", "/v1/admin/tills", {"till": HOST}, jwt=team.jwt())[1]["secret"]
    call("POST", f"/v1/admin/tills/{HOST}/san", {}, jwt=team.jwt())
    iss.ensure_all()
    assert list((tmp_path / "certs").glob("extra__*"))
    assert call("DELETE", f"/v1/admin/tills/{HOST}", jwt=team.jwt()) == (200, {"till": HOST, "removed": True})
    assert iss.for_till(s1)[0] == 401
    assert not list((tmp_path / "certs").glob("extra__*"))
    assert call("DELETE", f"/v1/admin/tills/{HOST}", jwt=team.jwt()) == (200, {"till": HOST, "removed": False})


def test_bad_body(stack):
    _, team, call, _ = stack
    assert call("POST", "/v1/admin/tills", [1, 2], jwt=team.jwt())[0] == 400
    assert call("POST", "/v1/admin/other", {}, jwt=team.jwt())[0] == 404


def test_keys_are_cached(stack):
    _, team, call, _ = stack
    for _ in range(3):
        call("POST", "/v1/admin/tills", {"till": HOST}, jwt=team.jwt())
    assert team.fetches == 1


def test_acme_stays_on_staging_by_default(monkeypatch, tmp_path):
    """Г2: production на LE е изрично решение на Росен — по подразбиране staging."""
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        raise RuntimeError("stop")

    monkeypatch.delenv("ACME_SERVER", raising=False)
    monkeypatch.setenv("CLOUDFLARE_DNS_API_TOKEN", "x")
    monkeypatch.setenv("ACME_EMAIL", "x@example.com")
    monkeypatch.setenv("CERT_ISSUER_DATA", str(tmp_path))
    monkeypatch.setattr(m.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        m.lego_acme(["*.erpnet.mcpworks.net"])
    assert seen["cmd"][seen["cmd"].index("--server") + 1] == m.LE_STAGING
