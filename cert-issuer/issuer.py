"""Сертификатният сървър за касите (ADR erpnet-kasi-fleet/0005).

Модел (Росен, 29.10.2026): ЕДИН сертификат за всички машини, от Let's Encrypt (DNS-01,
без запис с адрес). Носи `*.erpnet.mcpworks.net` и по едно `*.<клиент>.erpnet.mcpworks.net`
за всеки клиент с записана машина — `*` покрива само едно ниво (RFC 6125), а машините
са на две: `kasa.shop1.erpnet.mcpworks.net`. Сървърът сам се грижи да го има: издава,
подновява под RENEW_DAYS дни и преиздава, щом се появи нов клиент. Машините (проксито
на касата) винаги го ДЪРПАТ оттук и го слагат пред устройствата си. Токенът за DNS е
само тук.

Контрол: всяка машина е с тайна (`add-till`), всяко теглене се записва (машина,
кога, адрес, сериен номер на сертификата) — `GET /` и `python issuer.py pulls`.
Прегледът `GET /` иска Cloudflare Access (ACCESS_ENABLED=1) или OVERVIEW_TOKEN.
`remove-till` спира машината.

    GET  /v1/cert     Authorization: Bearer <тайната на машината>
                      → {"names", "chain", "key", "notAfter", "serial"}   (503, докато се издава)
    GET  /            кои машини кога са теглили (зад Access на тунела)
    GET  /healthz

    Admin API (ADR erpnet-kasi-fleet/0012, за услугата till-enroller) — само зад Cloudflare
    Access със service token: заглавката Cf-Access-Jwt-Assertion се проверява тук (подпис
    срещу ключовете на екипа, aud, iss, срок, common_name = разрешен service token).
    Без нея → 401. Ключът на касата е cf_hostname (`<каса>.<клиент>.<CERT_BASE>`).

    POST   /v1/admin/tills              {"till": "<cf_hostname>"} → {"till", "secret"} (тайната веднъж)
    DELETE /v1/admin/tills/<cf_hostname>                          → {"till", "removed"}
    POST   /v1/admin/tills/<cf_hostname>/san                       → {"till", "extra"}
                                         добавя `<каса>.<клиент>.<LAN_ZONE>` (ADR-0013)

    python issuer.py serve
    python issuer.py add-till <клиент>/<каса>        # печата тайната веднъж
    python issuer.py rotate-till <клиент>/<каса>
    python issuer.py remove-till <клиент>/<каса>
    python issuer.py set-extra <клиент>/<каса> <име> …  # публични имена само за тази машина
                                                     # (отделен сертификат; зоните — EXTRA_ZONES)
    python issuer.py ensure                          # издай/поднови сега, без сървър
    python issuer.py pulls                           # кой кога е теглил

Env: CERT_ISSUER_DATA (/data), CERT_BASE (erpnet.mcpworks.net), CLOUDFLARE_DNS_API_TOKEN, ACME_EMAIL,
ACME_SERVER (по подразбиране staging — production се пуска изрично),
PORT (8095), CHECK_EVERY_S (21600), LAN_ZONE (lan.mcpworks.net),
ACCESS_TEAM_DOMAIN (<екип>.cloudflareaccess.com), ACCESS_ADMIN_AUD (AUD на приложението в Access),
ACCESS_ADMIN_CLIENT_IDS (Client ID-тата на разрешените service token-и, със запетая).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
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
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger("cert-issuer")

LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
LE_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
RENEW_DAYS = 30
MAX_PULLS_KEPT = 2000
MAX_ADMIN_BODY = 4096
# Slug по договора v1 (CONTRACT_v1.md, `till` и `erpnet_slug`).
SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class AccessVerifier:
    """Проверка на Cf-Access-Jwt-Assertion за admin API (ADR erpnet-kasi-fleet/0012).

    Заглавката сама по себе си не значи нищо: всеки в мрежата на контейнера може да я
    сложи. Затова тук се проверява подписът (RS256) срещу ключовете на екипа
    (`https://<екип>/cdn-cgi/access/certs`), `aud`, `iss`, срокът и че токенът е на
    разрешен service token (`common_name` = Client ID). Без настройка — всичко е отказ.
    `fetch_keys() -> {"keys": [...]}` в тестовете е подменен.
    """

    KEYS_TTL = 3600
    LEEWAY = 60

    def __init__(self, team_domain: str, aud: str, client_ids: tuple[str, ...],
                 fetch_keys: Optional[Callable[[], dict]] = None,
                 now: Callable[[], float] = time.time):
        self.team = team_domain.strip().removeprefix("https://").strip("/")
        self.aud = aud.strip()
        self.client_ids = tuple(c.strip() for c in client_ids if c.strip())
        self.fetch_keys = fetch_keys or self._fetch_keys
        self.now = now
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._keys_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "AccessVerifier":
        return cls(os.environ.get("ACCESS_TEAM_DOMAIN", ""), os.environ.get("ACCESS_ADMIN_AUD", ""),
                   tuple(os.environ.get("ACCESS_ADMIN_CLIENT_IDS", "").split(",")))

    @property
    def configured(self) -> bool:
        return bool(self.team and self.aud and self.client_ids)

    def _fetch_keys(self) -> dict:
        import urllib.request
        with urllib.request.urlopen(f"https://{self.team}/cdn-cgi/access/certs", timeout=10) as r:
            return json.load(r)

    def _key(self, kid: str) -> Optional[rsa.RSAPublicKey]:
        with self._lock:
            fresh = self.now() - self._keys_at < self.KEYS_TTL
            if kid in self._keys and fresh:
                return self._keys[kid]
            # Непознат kid (ротация на ключовете) — ново теглене, но не по-често от 1/мин.
            if fresh and self.now() - self._keys_at < 60:
                return self._keys.get(kid)
            try:
                jwks = self.fetch_keys()
            except Exception:  # noqa: BLE001
                log.warning("access: cannot fetch the team keys")
                return self._keys.get(kid)
            keys = {}
            for k in jwks.get("keys", []):
                if k.get("kty") == "RSA" and k.get("kid"):
                    keys[k["kid"]] = rsa.RSAPublicNumbers(
                        int.from_bytes(_b64u_dec(k["e"]), "big"),
                        int.from_bytes(_b64u_dec(k["n"]), "big")).public_key()
            self._keys, self._keys_at = keys, self.now()
            return keys.get(kid)

    def verify(self, token: str) -> Optional[dict]:
        """Съдържанието на токена, ако е валиден и от разрешен service token; иначе None."""
        if not (self.configured and token):
            return None
        try:
            h64, p64, s64 = token.split(".")
            header = json.loads(_b64u_dec(h64))
            claims = json.loads(_b64u_dec(p64))
            if header.get("alg") != "RS256":
                return None
            key = self._key(str(header.get("kid", "")))
            if key is None:
                return None
            key.verify(_b64u_dec(s64), f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, TypeError, InvalidSignature, json.JSONDecodeError):
            return None
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        now = self.now()
        if (self.aud not in auds
                or claims.get("iss") != f"https://{self.team}"
                or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] + self.LEEWAY < now
                or claims.get("nbf", 0) - self.LEEWAY > now
                or not any(hmac.compare_digest(str(claims.get("common_name", "")), c) for c in self.client_ids)):
            return None
        return claims


class Issuer:
    """Регистърът на машините, сертификатът, грижата за него и отчетът кой го е теглил.

    `acme([имена]) -> (chain_pem, key_pem)` издава; в тестовете е подменен.
    """

    def __init__(self, data_dir: Path, acme: Callable[[list[str]], tuple[bytes, bytes]],
                 base: str = "erpnet.mcpworks.net",
                 now: Callable[[], float] = time.time,
                 extra_zones: tuple[str, ...] = (),
                 lan_zone: str = "lan.mcpworks.net"):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.certs = self.data / "certs"
        self.certs.mkdir(exist_ok=True)
        os.chmod(self.certs, 0o700)
        self.acme = acme
        self.base = base.lower().strip(".")
        self.now = now
        self._lock = threading.Lock()
        self._issuing: set[str] = set()
        # Зони, в които машина може да има допълнителни (публични) имена — само тук има DNS.
        self.extra_zones = tuple(z.lower().strip(".") for z in extra_zones if z.strip())
        # Поддървото за LAN имената на касите (ADR-0013): `<каса>.<клиент>.<lan_zone>`.
        self.lan_zone = lan_zone.lower().strip(".")

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

    def check_host(self, host: str) -> str:
        """Ключът на каса от admin API: cf_hostname `<каса>.<клиент>.<база>` (ADR-0012)."""
        host = (host or "").strip()
        labels = host.removesuffix("." + self.base).split(".") if host.endswith("." + self.base) else []
        if len(labels) != 2 or not all(SLUG_RE.match(lbl) for lbl in labels):
            raise ValueError(f"till must be '<kasa>.<client>.{self.base}' (slugs ^[a-z0-9-]{{1,30}}$)")
        return host

    def _client_of(self, till: str) -> str:
        """Клиентът на машината: `<клиент>/<каса>` (ръчно, add-till) или cf_hostname (admin API)."""
        if "/" in till:
            return till.split("/")[0]
        return till.removesuffix("." + self.base).split(".")[1]

    def add_host(self, host: str) -> str:
        """Admin API: касата по cf_hostname; тайната се показва веднъж, пази се само sha256."""
        return self.add_till(self.check_host(host), checked=True)

    def add_lan_name(self, host: str) -> list[str]:
        """Допълнителното име `<каса>.<клиент>.<lan_zone>` в сертификата на тази машина (ADR-0013).

        Името се извежда от cf_hostname, не се приема отвън — затова не минава през EXTRA_ZONES.
        """
        host = self.check_host(host)
        till, client = host.removesuffix("." + self.base).split(".")
        name = f"{till}.{client}.{self.lan_zone}"
        with self._lock:
            reg = self._load()
            if host not in reg["tills"]:
                raise KeyError(host)
            extra = sorted(set(reg["tills"][host].get("extra", [])) | {name})
            reg["tills"][host]["extra"] = extra
            self._save(reg)
        return extra

    def add_till(self, till: str, checked: bool = False) -> str:
        """Нова (или сменена) тайна на машината; показва се веднъж."""
        if not checked:
            self._check_name(till)
        secret = secrets.token_urlsafe(32)
        with self._lock:
            reg = self._load()
            t = reg["tills"].get(till, {})
            t.update({"secret_sha256": _sha256(secret), "created": t.get("created", int(self.now()))})
            reg["tills"][till] = t          # смяна на тайната пази допълнителните имена
            self._save(reg)
        return secret

    def set_extra(self, till: str, names: list[str]) -> list[str]:
        """Допълнителни публични имена САМО за тази машина (отделен сертификат, само за нея)."""
        clean = []
        for n in names:
            n = n.lower().strip(".")
            label_ok = all(lbl and lbl.replace("-", "").isalnum() for lbl in n.removeprefix("*.").split("."))
            zone_ok = any(n.endswith("." + z) for z in self.extra_zones)
            if not label_ok or not zone_ok or n.endswith("." + self.base):
                raise ValueError(f"{n}: not allowed (zones: {', '.join(self.extra_zones) or 'none'})")
            clean.append(n)
        with self._lock:
            reg = self._load()
            if till not in reg["tills"]:
                raise ValueError(f"{till}: unknown machine")
            reg["tills"][till]["extra"] = sorted(set(clean))
            self._save(reg)
        return sorted(set(clean))

    def remove_till(self, till: str) -> bool:
        with self._lock:
            reg = self._load()
            gone = reg["tills"].pop(till, None) is not None
            self._save(reg)
            # Отделният сертификат на машината (допълнителните имена) не остава на диска.
            for p in self._paths("extra:" + till):
                p.unlink(missing_ok=True)
        return gone

    def _till_by_secret(self, reg: dict, secret: str) -> Optional[str]:
        h = _sha256(secret)
        found = None
        for name, t in reg["tills"].items():
            if hmac.compare_digest(t["secret_sha256"], h):
                found = name
        return found

    # ─── сертификатът ─────────────────────────────────────────────────

    def _paths(self, key: str) -> tuple[Path, Path]:
        stem = "wildcard" if key == "main" else "extra__" + key.split(":", 1)[1].replace("/", "__")
        return self.certs / f"{stem}.crt", self.certs / f"{stem}.key"

    @property
    def _crt(self) -> Path:
        return self._paths("main")[0]

    @property
    def _key(self) -> Path:
        return self._paths("main")[1]

    def _wanted(self, key: str) -> list[str]:
        if key == "main":
            return self.wanted_names()
        t = self._load()["tills"].get(key.split(":", 1)[1], {})
        return list(t.get("extra", []))

    def _read(self, key: str) -> Optional[dict]:
        crt, keyf = self._paths(key)
        if not (crt.exists() and keyf.exists()):
            return None
        leaf = x509.load_pem_x509_certificate(crt.read_bytes())
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
            .get_values_for_type(x509.DNSName)
        return {"names": names, "chain": crt.read_text(), "key": keyf.read_text(),
                "notAfter": int(leaf.not_valid_after_utc.timestamp()), "serial": format(leaf.serial_number, "x")}

    def _needs(self, key: str) -> bool:
        wanted = self._wanted(key)
        if not wanted:
            return False
        cur = self._read(key)
        return (cur is None or cur["notAfter"] - self.now() <= RENEW_DAYS * 86400
                or not set(wanted) <= set(cur["names"]))

    def _ensure_key(self, key: str) -> bool:
        with self._lock:
            if key in self._issuing or not self._needs(key):
                return False
            self._issuing.add(key)
        try:
            wanted = self._wanted(key)
            chain, pkey = self.acme(wanted)
            leaf = x509.load_pem_x509_certificate(chain)
            got = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
                .get_values_for_type(x509.DNSName)
            if not set(wanted) <= set(got):
                raise RuntimeError(f"ACME returned a certificate for {got}, not {wanted}")
            crt, keyf = self._paths(key)
            for p, data in ((keyf, pkey), (crt, chain)):
                tmp = p.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.chmod(tmp, 0o600)
                tmp.replace(p)
            log.info("issued %s (%s) until %s", ",".join(got), key, leaf.not_valid_after_utc.date())
            return True
        finally:
            with self._lock:
                self._issuing.discard(key)

    def extra_of(self, till: str) -> Optional[dict]:
        return self._read("extra:" + till)

    def wanted_names(self) -> list[str]:
        """`*.<база>` + `*.<клиент>.<база>` за всеки клиент с записана машина."""
        clients = sorted({self._client_of(t) for t in self._load()["tills"]})
        return [f"*.{self.base}"] + [f"*.{c}.{self.base}" for c in clients]

    def current(self) -> Optional[dict]:
        return self._read("main")

    def needs_issue(self) -> bool:
        return self._needs("main")

    def ensure(self) -> bool:
        """Общият: издава/подновява, ако трябва. True — издаден нов."""
        return self._ensure_key("main")

    def ensure_all(self) -> None:
        """Общият и допълнителните на всички машини; един провал не спира другите."""
        keys = ["main"] + ["extra:" + t for t, v in self._load()["tills"].items() if v.get("extra")]
        for k in keys:
            try:
                self._ensure_key(k)
            except Exception:  # noqa: BLE001
                log.exception("ensure %s failed", k)

    def _ensure_in_background(self) -> None:
        threading.Thread(target=self._ensure_logged, daemon=True).start()

    def _ensure_logged(self) -> None:
        self.ensure_all()

    # ─── тегленето ────────────────────────────────────────────────────

    def for_till(self, secret: str, address: str = "") -> tuple[int, dict]:
        with self._lock:
            reg = self._load()
            till = self._till_by_secret(reg, secret)
        if not till:
            return 401, {"detail": "unknown machine"}
        cur = self.current()
        client = self._client_of(till)
        if cur is None or f"*.{client}.{self.base}" not in cur["names"]:
            # Сертификат няма или още не носи името на този клиент — издава се;
            # старият (ако има) остава в сила за другите машини.
            self._ensure_in_background()
            return 503, {"detail": "certificate is being issued, try again in a minute"}
        extra = None
        if self._wanted("extra:" + till):
            extra = self.extra_of(till)
            if extra is None or self._needs("extra:" + till):
                self._ensure_in_background()
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
        body = dict(cur)
        if extra is not None:
            body["extra"] = extra          # само на тази машина
        return 200, body

    def overview(self) -> dict:
        reg, cur = self._load(), self.current()
        return {
            "names": self.wanted_names(),
            "certificate": None if cur is None else {"serial": cur["serial"], "notAfter": cur["notAfter"],
                                                     "names": cur["names"]},
            "machines": {k: {**{x: v.get(x) for x in ("created", "last_pull", "last_serial", "last_address")},
                             "extra": v.get("extra", []),
                             "extra_notAfter": (self.extra_of(k) or {}).get("notAfter")}
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
        head = (f"{html.escape(', '.join(cert['names']))} — serial {cert['serial']}, until {_ts(cert['notAfter'])}"
                if cert else "not issued yet")
        return ("<!doctype html><meta charset=utf-8><title>cert-issuer</title>"
                "<style>body{font:14px system-ui;margin:24px}td,th{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left}</style>"
                f"<h2>{head}</h2><table><tr><th>machine</th><th>added</th><th>last pull</th><th>from</th><th>has</th></tr>{rows}</table>")


# ─── ACME през lego ────────────────────────────────────────────────────

def lego_acme(domains: list[str]) -> tuple[bytes, bytes]:
    """`lego` с Cloudflare DNS-01; ключът EC P-256 се прави тук, на сървъра."""
    for var in ("CLOUDFLARE_DNS_API_TOKEN", "ACME_EMAIL"):
        if not os.environ.get(var):
            raise RuntimeError(f"issuer has no {var}")
    data = Path(os.environ.get("CERT_ISSUER_DATA", "/data")) / "lego"
    cmd = ["lego", "--accept-tos", "--email", os.environ["ACME_EMAIL"],
           "--server", os.environ.get("ACME_SERVER", LE_STAGING),
           "--dns", "cloudflare", "--dns.resolvers", "1.1.1.1:53", "--key-type", "ec256",
           *[a for d in domains for a in ("--domains", d)], "--path", str(data), "run"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        tail = " ".join((r.stderr.strip().splitlines() or ["?"])[-1].split())[-700:]
        raise RuntimeError("ACME failed: " + tail)
    base = data / "certificates" / domains[0].replace("*", "_")  # lego кръщава по първото име
    return Path(f"{base}.crt").read_bytes(), Path(f"{base}.key").read_bytes()


# ─── HTTP ──────────────────────────────────────────────────────────────

def make_handler(issuer: Issuer, access: Optional[AccessVerifier] = None):
    access = access or AccessVerifier.from_env()

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
                # Прегледът: човек, пуснат от Cloudflare Access (само ако пред нас
                # наистина има Access — иначе заглавката може да се подправи), или
                # ключът за преглед.
                via_access = (os.environ.get("ACCESS_ENABLED") == "1"
                              and self.headers.get("Cf-Access-Authenticated-User-Email"))
                tok = os.environ.get("OVERVIEW_TOKEN", "")
                auth = self.headers.get("Authorization", "")
                via_token = bool(tok) and hmac.compare_digest(auth, "Bearer " + tok)
                if not (via_access or via_token):
                    return self._json(403, {"detail": "overview: Cloudflare Access or the overview token"})
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

        # ─── admin API (till-enroller, ADR-0012) ─────────────────────────

        def _admin(self) -> Optional[str]:
            """Пътят след /v1/admin/tills, ако заявката е от разрешен service token; иначе отговаря сама."""
            if not (self.path == "/v1/admin/tills" or self.path.startswith("/v1/admin/tills/")):
                self._json(404, {"detail": "Not Found"})
                return None
            claims = access.verify(self.headers.get("Cf-Access-Jwt-Assertion", ""))
            if claims is None:
                self._json(401, {"detail": "admin: Cloudflare Access service token required"})
                return None
            log.info("admin %s %s by %s", self.command, self.path, claims.get("common_name"))
            return self.path[len("/v1/admin/tills"):]

        def _body(self) -> Optional[dict]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_ADMIN_BODY:
                self._json(413, {"detail": "body too large"})
                return None
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._json(400, {"detail": "JSON object expected"})
                return None
            return body

        def do_POST(self):  # noqa: N802
            rest = self._admin()
            if rest is None:
                return None
            body = self._body()
            if body is None:
                return None
            try:
                if rest == "":
                    host = issuer.check_host(str(body.get("till", "")))
                    return self._json(201, {"till": host, "secret": issuer.add_host(host)})
                if rest.endswith("/san") and rest.count("/") == 2:
                    host = rest[1:-len("/san")]
                    return self._json(200, {"till": host, "extra": issuer.add_lan_name(host)})
            except ValueError as e:
                return self._json(400, {"detail": str(e)})
            except KeyError:
                return self._json(404, {"detail": "unknown machine"})
            return self._json(404, {"detail": "Not Found"})

        def do_DELETE(self):  # noqa: N802
            rest = self._admin()
            if rest is None:
                return None
            if not rest.startswith("/") or rest.count("/") != 1:
                return self._json(404, {"detail": "Not Found"})
            try:
                host = issuer.check_host(rest[1:])
            except ValueError as e:
                return self._json(400, {"detail": str(e)})
            # Идемпотентно: retire и rollback-ът на till-enroller може да го повторят.
            return self._json(200, {"till": host, "removed": issuer.remove_till(host)})

    return Handler


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data = Path(os.environ.get("CERT_ISSUER_DATA", "/data"))
    issuer = Issuer(data, lego_acme, os.environ.get("CERT_BASE", "erpnet.mcpworks.net"),
                    extra_zones=tuple(os.environ.get("EXTRA_ZONES", "").split(",")),
                    lan_zone=os.environ.get("LAN_ZONE", "lan.mcpworks.net"))
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        every = int(os.environ.get("CHECK_EVERY_S", "21600"))

        def keeper():
            while True:
                issuer._ensure_logged()
                time.sleep(every)

        threading.Thread(target=keeper, daemon=True, name="keeper").start()
        port = int(os.environ.get("PORT", "8095"))
        access = AccessVerifier.from_env()
        srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(issuer, access))
        log.info("listening on :%d, %s, ACME %s, admin API %s", port, ",".join(issuer.wanted_names()),
                 os.environ.get("ACME_SERVER", LE_STAGING), "on" if access.configured else "off (no Access settings)")
        srv.serve_forever()
        return 0
    if cmd == "ensure":
        issuer.ensure_all()          # общият и допълнителните; в този процес, не на заден план
        return 0
    if cmd == "pulls":
        print(json.dumps(issuer.overview(), indent=2))
        return 0
    if cmd in ("add-till", "rotate-till") and len(argv) == 2:
        print(issuer.add_till(argv[1]))
        return 0
    if cmd == "set-extra" and len(argv) >= 2:
        print(json.dumps(issuer.set_extra(argv[1], argv[2:])))
        # Издаването: от сървъра (keeper) или `issuer.py ensure` — фонов поток тук би
        # умрял заедно с командата.
        return 0
    if cmd == "remove-till" and len(argv) == 2:
        return 0 if issuer.remove_till(argv[1]) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
