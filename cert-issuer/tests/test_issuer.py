"""cert-issuer: един `*.erpnet.mcpworks.net`, сървърът го държи, машините го дърпат, всяко теглене се записва.

Let's Encrypt е подменен с местен CA (`FakeACME`)."""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issuer as m  # noqa: E402

WILD = "*.erpnet.mcpworks.net"


class FakeACME:
    def __init__(self, days=90):
        self.days = days
        self.calls = []
        self.wrong_name = None
        self.ca = ec.generate_private_key(ec.SECP256R1())

    def __call__(self, domain):
        self.calls.append(domain)
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
                .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake")]))
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now).not_valid_after(now + timedelta(days=self.days))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(self.wrong_name or domain)]), False)
                .sign(self.ca, hashes.SHA256()))
        return (cert.public_bytes(serialization.Encoding.PEM),
                key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    acme, clock = FakeACME(), Clock()
    iss = m.Issuer(tmp_path, acme, now=clock)
    s1 = iss.add_till("shop1/kasa1")
    s2 = iss.add_till("shop2/kasa1")
    return iss, acme, clock, s1, s2


def test_only_the_hash_of_the_secret_is_kept(env, tmp_path):
    iss, _, _, s1, _ = env
    raw = (tmp_path / "registry.json").read_text()
    assert s1 not in raw and oct((tmp_path / "registry.json").stat().st_mode)[-3:] == "600"


def test_one_certificate_for_every_machine(env, tmp_path):
    iss, acme, _, s1, s2 = env
    assert iss.ensure() is True
    a, b = iss.for_till(s1, "1.2.3.4"), iss.for_till(s2, "5.6.7.8")
    assert a[0] == b[0] == 200 and a[1] == b[1] and acme.calls == [WILD]
    leaf = x509.load_pem_x509_certificate(a[1]["chain"].encode())
    assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [WILD]
    assert a[1]["key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert oct((tmp_path / "certs" / "wildcard.key").stat().st_mode)[-3:] == "600"


def test_every_pull_is_recorded(env):
    iss, _, clock, s1, _ = env
    iss.ensure()
    serial = iss.current()["serial"]
    iss.for_till(s1, "10.0.0.5")
    clock.t += 3600
    iss.for_till(s1, "10.0.0.6")
    o = iss.overview()
    assert [(p["till"], p["address"], p["serial"]) for p in o["pulls"]] == [
        ("shop1/kasa1", "10.0.0.5", serial), ("shop1/kasa1", "10.0.0.6", serial)]
    assert o["machines"]["shop1/kasa1"]["last_address"] == "10.0.0.6"
    assert o["machines"]["shop2/kasa1"]["last_pull"] is None
    assert "kasa1" in iss.overview_html() and "never" in iss.overview_html()


def test_unknown_or_removed_machine_gets_nothing_and_is_not_recorded(env):
    iss, _, _, s1, _ = env
    iss.ensure()
    assert iss.for_till("nope")[0] == 401
    iss.remove_till("shop1/kasa1")
    assert iss.for_till(s1)[0] == 401
    assert iss.overview()["pulls"] == []


def test_no_certificate_yet_is_503_and_starts_issuing(env):
    iss, acme, _, s1, _ = env
    st, body = iss.for_till(s1)
    assert st == 503 and "key" not in body
    for _ in range(100):
        if iss.current():
            break
        time.sleep(0.02)
    assert acme.calls == [WILD] and iss.for_till(s1)[0] == 200


def test_renewed_only_when_30_days_or_less_are_left(env):
    iss, acme, clock, *_ = env
    assert iss.ensure() is True
    assert iss.ensure() is False
    clock.t += 59 * 86400
    assert iss.ensure() is False
    clock.t += 86400 + 1
    assert iss.ensure() is True and acme.calls == [WILD, WILD]


def test_a_certificate_for_another_name_is_not_stored(env):
    iss, acme, *_ = env
    acme.wrong_name = "*.evil.example"
    with pytest.raises(RuntimeError):
        iss.ensure()
    assert iss.current() is None


def test_bad_machine_name():
    with pytest.raises(ValueError):
        m.Issuer._check_name("Shop1/kasa 1")


def test_http(env, monkeypatch):
    iss, _, _, s1, _ = env
    iss.ensure()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.make_handler(iss))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"

    def get(path, headers=None):
        req = urllib.request.Request(url + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    try:
        assert get("/v1/cert")[0] == 401
        assert get("/v1/cert", {"Authorization": "Bearer nope"})[0] == 401
        st, body = get("/v1/cert", {"Authorization": "Bearer " + s1, "CF-Connecting-IP": "203.0.113.7"})
        assert st == 200 and json.loads(body)["names"] == [WILD]
        assert iss.overview()["pulls"][-1]["address"] == "203.0.113.7"
        assert get("/")[0] == 403 and get("/v1/overview")[0] == 403
        # без Access пред нас заглавката не значи нищо (може да се подправи)
        assert get("/", {"Cf-Access-Authenticated-User-Email": "x@example.com"})[0] == 403
        monkeypatch.setenv("ACCESS_ENABLED", "1")
        st, body = get("/", {"Cf-Access-Authenticated-User-Email": "rosen@example.com"})
        assert st == 200 and b"shop1/kasa1" in body
        monkeypatch.delenv("ACCESS_ENABLED")
        monkeypatch.setenv("OVERVIEW_TOKEN", "ov-tok")
        assert get("/v1/overview", {"Authorization": "Bearer nope"})[0] == 403
        assert get("/v1/overview", {"Authorization": "Bearer ov-tok"})[0] == 200
        assert get("/healthz") == (200, b'{"ok": true}')
    finally:
        srv.shutdown()
