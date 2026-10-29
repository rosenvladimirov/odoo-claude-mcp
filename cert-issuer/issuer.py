"""Сертификатният сървър за касите (ADR erpnet-kasi-fleet/0005).

Модел (Росен, 29.10.2026): ЕДИН wildcard сертификат за всички — `*.erpnet.mcpworks.net`
от Let's Encrypt (DNS-01, без запис с адрес). В публичните дневници (CT) се вижда само
той — без имена на клиенти, устройства или IP. Сървърът сам се грижи да го има
(издава и подновява под RENEW_DAYS дни); машините (проксито на касата) винаги го
ДЪРПАТ оттук през тунела на mcpworks.net и го слагат пред устройствата си
(`bluecash-shop1.erpnet.mcpworks.net` → BlueCash в LAN). Токенът за DNS е само тук.

Контрол: всяка машина е с тайна (`add-till`), всяко теглене се записва (машина,
кога, адрес, сериен номер на сертификата) — `GET /` и `python issuer.py pulls`.
`remove-till` спира машината.

    GET  /v1/cert     Authorization: Bearer <тайната на машината>
                      → {"names", "chain", "key", "notAfter", "serial"}   (503, докато се издава)
    GET  /            кои машини кога са теглили (зад Access на тунела)
    GET  /healthz

    python issuer.py serve
    python issuer.py add-till <клиент>/<каса>        # печата тайната веднъж
    python issuer.py rotate-till <клиент>/<каса>
    python issuer.py remove-till <клиент>/<каса>
    python issuer.py ensure                          # издай/поднови сега, без сървър
    python issuer.py pulls                           # кой кога е теглил

Env: CERT_ISSUER_DATA (/data), CERT_DOMAIN (*.erpnet.mcpworks.net), CLOUDFLARE_DNS_API_TOKEN, ACME_EMAIL,
ACME_SERVER (по подразбиране staging — production се пуска изрично),
PORT (8095), CHECK_EVERY_S (21600).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from cryptography import x509

log = logging.getLogger("cert-issuer")

LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
LE_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
RENEW_DAYS = 30
MAX_PULLS_KEPT = 2000


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Issuer:
    """Регистърът на машините, сертификатът, грижата за него и отчетът кой го е теглил.

    `acme(domain) -> (chain_pem, key_pem)` издава; в тестовете е подменен.
    """

    def __init__(self, data_dir: Path, acme: Callable[[str], tuple[bytes, bytes]],
                 domain: str = "*.erpnet.mcpworks.net",
                 now: Callable[[], float] = time.time):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.certs = self.data / "certs"
        self.certs.mkdir(exist_ok=True)
        os.chmod(self.certs, 0o700)
        self.acme = acme
        self.domain = domain.lower()
        self.now = now
        self._lock = threading.Lock()
        self._issuing = False

    # ─── регистърът на машините ───────────────────────────────────────

    @property
    def _registry_path(self) -> Path:
        return self.data / "registry.json"

    def _load(self) -> dict:
        try:
            reg = json.loads(self._registry_path.read_text())
        except FileNotFoundError:
            reg = {}
        reg.setdefault("tills", {})
        reg.setdefault("pulls", [])
        return reg

    def _save(self, reg: dict) -> None:
        tmp = self._registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, indent=2, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(self._registry_path)

    @staticmethod
    def _check_name(till: str) -> str:
        parts = till.split("/")
        ok = len(parts) == 2 and all(p and p.replace("-", "").isalnum() and p == p.lower() for p in parts)
        if not ok:
            raise ValueError("till must be '<client>/<kasa>' in lower case, letters/digits/dash")
        return till

    def add_till(self, till: str) -> str:
        """Нова (или сменена) тайна на машината; показва се веднъж."""
        self._check_name(till)
        secret = secrets.token_urlsafe(32)
        with self._lock:
            reg = self._load()
            reg["tills"][till] = {"secret_sha256": _sha256(secret), "created": int(self.now())}
            self._save(reg)
        return secret

    def remove_till(self, till: str) -> bool:
        with self._lock:
            reg = self._load()
            gone = reg["tills"].pop(till, None) is not None
            self._save(reg)
        return gone

    def _till_by_secret(self, reg: dict, secret: str) -> Optional[str]:
        h = _sha256(secret)
        found = None
        for name, t in reg["tills"].items():
            if hmac.compare_digest(t["secret_sha256"], h):
                found = name
        return found

    # ─── сертификатът ─────────────────────────────────────────────────

    @property
    def _crt(self) -> Path:
        return self.certs / "wildcard.crt"

    @property
    def _key(self) -> Path:
        return self.certs / "wildcard.key"

    def current(self) -> Optional[dict]:
        if not (self._crt.exists() and self._key.exists()):
            return None
        leaf = x509.load_pem_x509_certificate(self._crt.read_bytes())
        return {"names": [self.domain], "chain": self._crt.read_text(), "key": self._key.read_text(),
                "notAfter": int(leaf.not_valid_after_utc.timestamp()), "serial": format(leaf.serial_number, "x")}

    def needs_issue(self) -> bool:
        cur = self.current()
        return cur is None or cur["notAfter"] - self.now() <= RENEW_DAYS * 86400

    def ensure(self) -> bool:
        """Издава/подновява, ако трябва. True — издаден нов."""
        with self._lock:
            if self._issuing or not self.needs_issue():
                return False
            self._issuing = True
        try:
            chain, key = self.acme(self.domain)
            leaf = x509.load_pem_x509_certificate(chain)
            got = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
                .get_values_for_type(x509.DNSName)
            if self.domain not in got:
                raise RuntimeError(f"ACME returned a certificate for {got}, not {self.domain}")
            for p, data in ((self._key, key), (self._crt, chain)):
                tmp = p.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.chmod(tmp, 0o600)
                tmp.replace(p)
            log.info("issued %s until %s", self.domain, leaf.not_valid_after_utc.date())
            return True
        finally:
            with self._lock:
                self._issuing = False

    def _ensure_in_background(self) -> None:
        threading.Thread(target=self._ensure_logged, daemon=True).start()

    def _ensure_logged(self) -> None:
        try:
            self.ensure()
        except Exception:  # noqa: BLE001
            log.exception("ensure failed")

    # ─── тегленето ────────────────────────────────────────────────────

    def for_till(self, secret: str, address: str = "") -> tuple[int, dict]:
        with self._lock:
            reg = self._load()
            till = self._till_by_secret(reg, secret)
        if not till:
            return 401, {"detail": "unknown machine"}
        cur = self.current()
        if cur is None:
            self._ensure_in_background()
            return 503, {"detail": "certificate is being issued, try again in a minute"}
        if self.needs_issue():
            self._ensure_in_background()
        with self._lock:
            reg = self._load()
            if till in reg["tills"]:
                reg["tills"][till]["last_pull"] = int(self.now())
                reg["tills"][till]["last_serial"] = cur["serial"]
                reg["tills"][till]["last_address"] = address
                reg["pulls"].append({"till": till, "at": int(self.now()), "address": address, "serial": cur["serial"]})
                reg["pulls"] = reg["pulls"][-MAX_PULLS_KEPT:]
                self._save(reg)
        return 200, cur

    def overview(self) -> dict:
        reg, cur = self._load(), self.current()
        return {
            "domain": self.domain,
            "certificate": None if cur is None else {"serial": cur["serial"], "notAfter": cur["notAfter"]},
            "machines": {k: {x: v.get(x) for x in ("created", "last_pull", "last_serial", "last_address")}
                         for k, v in sorted(reg["tills"].items())},
            "pulls": reg["pulls"][-50:],
        }

    def overview_html(self) -> str:
        o = self.overview()
        cert = o["certificate"]
        rows = "".join(
            f"<tr><td>{html.escape(k)}</td><td>{_ts(v['created']) if v['created'] else '—'}</td>"
            f"<td>{_ts(v['last_pull']) if v.get('last_pull') else 'never'}</td>"
            f"<td>{html.escape(v.get('last_address') or '')}</td>"
            f"<td>{'current' if cert and v.get('last_serial') == cert['serial'] else html.escape(v.get('last_serial') or '—')}</td></tr>"
            for k, v in o["machines"].items())
        head = (f"{html.escape(o['domain'])} — serial {cert['serial']}, until {_ts(cert['notAfter'])}"
                if cert else f"{html.escape(o['domain'])} — not issued yet")
        return ("<!doctype html><meta charset=utf-8><title>cert-issuer</title>"
                "<style>body{font:14px system-ui;margin:24px}td,th{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left}</style>"
                f"<h2>{head}</h2><table><tr><th>machine</th><th>added</th><th>last pull</th><th>from</th><th>has</th></tr>{rows}</table>")


# ─── ACME през lego ────────────────────────────────────────────────────

def lego_acme(domain: str) -> tuple[bytes, bytes]:
    """`lego` с Cloudflare DNS-01; ключът EC P-256 се прави тук, на сървъра."""
    for var in ("CLOUDFLARE_DNS_API_TOKEN", "ACME_EMAIL"):
        if not os.environ.get(var):
            raise RuntimeError(f"issuer has no {var}")
    data = Path(os.environ.get("CERT_ISSUER_DATA", "/data")) / "lego"
    cmd = ["lego", "--accept-tos", "--email", os.environ["ACME_EMAIL"],
           "--server", os.environ.get("ACME_SERVER", LE_STAGING),
           "--dns", "cloudflare", "--dns.resolvers", "1.1.1.1:53", "--key-type", "ec256",
           "--domains", domain, "--path", str(data), "run"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        tail = " ".join((r.stderr.strip().splitlines() or ["?"])[-1].split())[-700:]
        raise RuntimeError("ACME failed: " + tail)
    base = data / "certificates" / domain.replace("*", "_")
    return Path(f"{base}.crt").read_bytes(), Path(f"{base}.key").read_bytes()


# ─── HTTP ──────────────────────────────────────────────────────────────

def make_handler(issuer: Issuer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cert-issuer/2"

        def _send(self, status: int, raw: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, status: int, body: dict) -> None:
            self._send(status, json.dumps(body).encode(), "application/json")

        def log_message(self, fmt, *args):  # без тайни в лога — само пътя и кода
            log.info("%s %s", self._address(), fmt % args)

        def _address(self) -> str:
            # През тунела истинският адрес идва в заглавка от Cloudflare.
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                return self._json(200, {"ok": True})
            if self.path in ("/", "/v1/overview"):
                # Прегледът е за човек, пуснат от Cloudflare Access; без него — нищо.
                who = self.headers.get("Cf-Access-Authenticated-User-Email")
                if not who and os.environ.get("OVERVIEW_WITHOUT_ACCESS") != "1":
                    return self._json(403, {"detail": "overview only through Cloudflare Access"})
                if self.path == "/":
                    return self._send(200, issuer.overview_html().encode(), "text/html; charset=utf-8")
                return self._json(200, issuer.overview())
            if self.path != "/v1/cert":
                return self._json(404, {"detail": "Not Found"})
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return self._json(401, {"detail": "missing bearer token"})
            status, body = issuer.for_till(auth[7:].strip(), self._address())
            return self._json(status, body)

    return Handler


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data = Path(os.environ.get("CERT_ISSUER_DATA", "/data"))
    issuer = Issuer(data, lego_acme, os.environ.get("CERT_DOMAIN", "*.erpnet.mcpworks.net"))
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        every = int(os.environ.get("CHECK_EVERY_S", "21600"))

        def keeper():
            while True:
                issuer._ensure_logged()
                time.sleep(every)

        threading.Thread(target=keeper, daemon=True, name="keeper").start()
        port = int(os.environ.get("PORT", "8095"))
        srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(issuer))
        log.info("listening on :%d, %s, ACME %s", port, issuer.domain, os.environ.get("ACME_SERVER", LE_STAGING))
        srv.serve_forever()
        return 0
    if cmd == "ensure":
        return 0 if issuer.ensure() or not issuer.needs_issue() else 1
    if cmd == "pulls":
        print(json.dumps(issuer.overview(), indent=2))
        return 0
    if cmd in ("add-till", "rotate-till") and len(argv) == 2:
        print(issuer.add_till(argv[1]))
        return 0
    if cmd == "remove-till" and len(argv) == 2:
        return 0 if issuer.remove_till(argv[1]) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
