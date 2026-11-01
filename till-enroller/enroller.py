"""Услугата за записване на касите (till-enroller, ADR erpnet-kasi-fleet/0012, договор v1).

Тя държи CF токена — не лаптопът за всяка каса и никога лицензионният сървър или каса.
Решения не взема: тегли от лицензионния сървър (LS) какво да направи и го прави.

Цикъл на ENR_INTERVAL_S (30) секунди — `pull`, после за всеки запис:

    create  1. тунел `erpnet-<клиент>-<каса>`
            2. маршрут по име `<каса>.<клиент>.erpnet.mcpworks.net` → тунела
            3. токен на тунела
            4. cert-issuer `POST /v1/admin/tills {till: cf_hostname}`
            5. LS `report` → grant_token, reg
            6. EFP1 v2 → запечатан с box_pub → LS `store`
            Провал по средата ⇒ rollback на създаденото, сверен със списъка в CF, и `done{ok:false}`.
    lan     DNS-only `A <каса>.<клиент>.lan.mcpworks.net → lan_ip` (кодът отказва всяко име извън
            *.lan.mcpworks.net — токенът има DNS Edit за цялата зона), SAN в cert-issuer, `done`.
    retire  маха маршрута, тунела, DNS записа и касата в cert-issuer, сверява с CF, `done`.

Всяко действие се отчита към LS с `done` (ok + бележка без тайни) — одитът е там.

Env (тайните — само оттук; нищо от тях не влиза в лога):
    CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID         Cloudflare (Tunnel Edit, hostname routes, DNS Edit)
    LS_URL, LS_API_KEY                               лицензионният сървър, потребител от group_till_provisioner
    CI_URL, CI_ACCESS_CLIENT_ID, CI_ACCESS_CLIENT_SECRET
                                                     cert-issuer през Access (service token)
    CI_PULL_URL                                      адресът на cert-issuer за касите (`cu` в EFP1), по подразбиране CI_URL
    ENR_SSH_PUBKEYS                                  SSH ключове за EFP1 `ssh` (по един на ред), по избор
    ENR_INTERVAL_S (30), ENR_DRY_RUN=1               сух режим: без CF повиквания, с фалшиви id-та
    ENR_ALIVE_FILE (/tmp/till-enroller.alive)        за HEALTHCHECK-а

    python enroller.py            # цикълът
    python enroller.py once       # един проход
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Mapping, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("till-enroller")

CF_API = "https://api.cloudflare.com/client/v4"
ERPNET_BASE = "erpnet.mcpworks.net"
# Единственото поддърво, в което услугата пише DNS (ADR-0013). Константа, не env: токенът има
# DNS Edit за цялата зона mcpworks.net (CF не дава права по поддърво), затова границата е тук.
LAN_SUFFIX = "lan.mcpworks.net"
SEAL_INFO = b"erpnet-efp1-seal-v1"
SLUG_RE = re.compile(r"^[a-z0-9-]{1,30}$")
# RFC1918 — същото като проверката на LS за `/till/lan`.
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
HTTP_TIMEOUT = 30


# ─── тайните не стигат до лога ─────────────────────────────────────────

class Redactor(logging.Filter):
    """Сменя всяка позната тайна с `***` в записа, преди да стигне до който и да е handler.

    Тайните се регистрират при зареждане (токените от env) и в движение (токените на тунелите).
    Това е втора линия: кодът и без това не логва тайни.
    """

    def __init__(self):
        super().__init__()
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, value: Optional[str]) -> None:
        if value and len(value) >= 6:
            with self._lock:
                self._secrets.add(value)

    def scrub(self, text: str) -> str:
        with self._lock:
            secrets_ = sorted(self._secrets, key=len, reverse=True)
        for s in secrets_:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = self.scrub(msg)
        if record.exc_info:
            # Трасето може да носи стойности от рамките — не го пускаме, само вида на грешката.
            clean += f" [{record.exc_info[0].__name__}]"
            record.exc_info = None
            record.exc_text = None
        record.msg, record.args = clean, ()
        return True


REDACT = Redactor()
log.addFilter(REDACT)


# ─── криптография по договора v1 ───────────────────────────────────────

def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical_json(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _raw_pub(k: X25519PrivateKey) -> bytes:
    return k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _seal_key(shared: bytes, eph_pub: bytes, box_pub: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, eph_pub + box_pub, SEAL_INFO).derive(shared)


def seal(box_pub: bytes, plaintext: bytes) -> str:
    """Запечатване с X25519 ключа на касата: b64u(eph_pub ‖ ChaCha20Poly1305(…)) — договор v1."""
    if len(box_pub) != 32:
        raise ValueError("box_pub must be 32 raw bytes")
    eph = X25519PrivateKey.generate()
    eph_pub = _raw_pub(eph)
    key = _seal_key(eph.exchange(X25519PublicKey.from_public_bytes(box_pub)), eph_pub, box_pub)
    return b64u(eph_pub + ChaCha20Poly1305(key).encrypt(b"\0" * 12, plaintext, None))


def unseal(box_private: bytes, sealed: str) -> bytes:
    """Обратното на seal — само за проверки; на сървъра частен ключ на каса няма."""
    raw = b64u_dec(sealed)
    eph_pub, ct = raw[:32], raw[32:]
    box = X25519PrivateKey.from_private_bytes(box_private)
    key = _seal_key(box.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, _raw_pub(box))
    return ChaCha20Poly1305(key).decrypt(b"\0" * 12, ct, None)


def build_efp1(client: str, till: str, host: str, tunnel_id: str, tunnel_token: str,
               reg: str, grant_token: str, cert_url: str, ssh: list[str], crt: Optional[str] = None) -> dict:
    """EFP1 v2 по договора (поправка 1.1). `crt` е тайната за certpull от cert-issuer — само тук,
    в запечатания EFP1. `d`/`fp` не се пращат: паролите идват по флота (`password_sealed`).
    Без `crt` (само векторите на договора v1) ключът липсва."""
    efp1 = {"v": 2, "c": client, "k": till, "h": host, "t": tunnel_id, "tok": tunnel_token,
            "reg": reg, "pt": grant_token, "cu": cert_url, "ssh": list(ssh), "cors": [reg]}
    if crt is not None:
        efp1["crt"] = crt
    return efp1


# ─── имената ───────────────────────────────────────────────────────────

class LanZoneError(ValueError):
    """DNS име извън *.lan.mcpworks.net — услугата не пише там."""


def check_lan_name(name: str) -> str:
    """Само `<каса>.<клиент>.lan.mcpworks.net` с slug-ове по договора; всичко друго — отказ."""
    name = (name or "").strip()
    if name != name.lower() or not name.endswith("." + LAN_SUFFIX):
        raise LanZoneError(f"DNS name outside *.{LAN_SUFFIX}: {name!r}")
    labels = name[: -len("." + LAN_SUFFIX)].split(".")
    if len(labels) != 2 or not all(SLUG_RE.match(lbl) for lbl in labels):
        raise LanZoneError(f"DNS name must be <till>.<client>.{LAN_SUFFIX}: {name!r}")
    return name


def lan_name(client: str, till: str) -> str:
    return check_lan_name(f"{till}.{client}.{LAN_SUFFIX}")


def tunnel_name(client: str, till: str) -> str:
    return f"erpnet-{client}-{till}"


def cf_hostname(client: str, till: str) -> str:
    return f"{till}.{client}.{ERPNET_BASE}"


def check_lan_ip(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(f"lan_ip is not an IP address: {ip!r}") from None
    if addr.version != 4 or not any(addr in n for n in PRIVATE_NETS):
        raise ValueError(f"lan_ip is not a private IPv4 (RFC1918): {ip}")
    return str(addr)


# ─── HTTP ──────────────────────────────────────────────────────────────

class HttpError(Exception):
    """Грешка от отсрещната страна; съобщението е без тайни (метод, път, код, код на грешка)."""

    def __init__(self, service: str, method: str, path: str, status: int, code: str = ""):
        self.status, self.code = status, code
        super().__init__(f"{service} {method} {path} -> {status}{' ' + code if code else ''}")


def _http(service: str, method: str, url: str, path: str, headers: dict,
          body=None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    hd = {"Content-Type": "application/json", "Accept": "application/json",
          "User-Agent": "till-enroller/1", **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=hd)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            b = json.loads(e.read() or b"null")
        except ValueError:
            b = None
        code = ""
        if isinstance(b, dict):
            if isinstance(b.get("error"), str):
                code = b["error"]
            elif b.get("errors"):
                code = ";".join(str(x.get("code", "")) for x in b["errors"] if isinstance(x, dict))
            elif isinstance(b.get("detail"), str):
                code = b["detail"][:120]
        raise HttpError(service, method, path, e.code, code) from None
    except urllib.error.URLError as e:
        raise HttpError(service, method, path, 0, f"unreachable ({e.reason})") from None


class LicenseServer:
    """Маршрутите `/l10n_bg/erpnet/enroller/*` на LS (договор v1, т. 9–12)."""

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self._auth = {"Authorization": "Bearer " + api_key}

    def _call(self, route: str, payload: dict) -> dict:
        path = "/l10n_bg/erpnet/enroller/" + route
        _, body = _http("LS", "POST", self.url + path, path, self._auth, payload)
        return body if isinstance(body, dict) else {}

    def pull(self) -> list[dict]:
        tills = self._call("pull", {}).get("tills") or []
        return [t for t in tills if isinstance(t, dict)]

    def report(self, till_ref: str, cf_tunnel_id: str, host: str) -> tuple[str, str]:
        b = self._call("report", {"till_ref": till_ref, "cf_tunnel_id": cf_tunnel_id, "cf_hostname": host})
        if not b.get("grant_token") or not b.get("reg"):
            raise RuntimeError("LS report: no grant_token/reg in the answer")
        return b["grant_token"], b["reg"]

    def store(self, till_ref: str, sealed: str) -> None:
        self._call("store", {"till_ref": till_ref, "sealed": sealed})

    def done(self, till_ref: str, action: str, ok: bool, note: str) -> None:
        self._call("done", {"till_ref": till_ref, "action": action, "ok": bool(ok), "note": REDACT.scrub(note)})


class CertIssuer:
    """Admin API на cert-issuer през Cloudflare Access (service token)."""

    def __init__(self, url: str, client_id: str, client_secret: str):
        self.url = url.rstrip("/")
        self._auth = {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret}

    def _call(self, method: str, path: str, body=None) -> dict:
        _, b = _http("CI", method, self.url + path, path, self._auth, body)
        return b if isinstance(b, dict) else {}

    def add(self, host: str) -> str:
        """Тайната за certpull (показва се веднъж). Отива само в запечатания EFP1 (`crt`)."""
        secret = self._call("POST", "/v1/admin/tills", {"till": host}).get("secret")
        if not secret:
            raise RuntimeError("cert-issuer add: no secret in the answer")
        REDACT.add(secret)
        return secret

    def remove(self, host: str) -> bool:
        return bool(self._call("DELETE", "/v1/admin/tills/" + host).get("removed"))

    def add_san(self, host: str) -> list[str]:
        return list(self._call("POST", f"/v1/admin/tills/{host}/san", {}).get("extra") or [])


class Cloudflare:
    """Тунели, маршрути по име и DNS. Пътищата са измерените на живо (ADR-0002, phase0)."""

    def __init__(self, token: str, account_id: str, zone_id: str, api: str = CF_API):
        self.api = api.rstrip("/")
        self.acc = f"/accounts/{account_id}"
        self.zone = f"/zones/{zone_id}"
        self._auth = {"Authorization": "Bearer " + token}

    def _cf(self, method: str, path: str, body=None):
        _, b = _http("CF", method, self.api + path, path.split("?")[0], self._auth, body)
        if isinstance(b, dict) and b.get("success") is False:
            raise HttpError("CF", method, path.split("?")[0], 200, "success=false")
        return b.get("result") if isinstance(b, dict) else None

    # тунели
    def tunnel_by_name(self, name: str) -> Optional[dict]:
        q = urllib.parse.urlencode({"name": name, "is_deleted": "false"})
        res = self._cf("GET", f"{self.acc}/cfd_tunnel?{q}") or []
        return next((t for t in res if t.get("name") == name), None)

    def create_tunnel(self, name: str) -> dict:
        return self._cf("POST", f"{self.acc}/cfd_tunnel", {"name": name, "config_src": "cloudflare"})

    def tunnel_token(self, tunnel_id: str) -> str:
        return self._cf("GET", f"{self.acc}/cfd_tunnel/{tunnel_id}/token")

    def delete_tunnel(self, tunnel_id: str) -> None:
        for path in (f"{self.acc}/cfd_tunnel/{tunnel_id}/connections", f"{self.acc}/cfd_tunnel/{tunnel_id}"):
            try:
                self._cf("DELETE", path)
            except HttpError as e:
                if e.status != 404:
                    raise

    # маршрути по име
    def routes(self) -> list[dict]:
        return self._cf("GET", f"{self.acc}/zerotrust/routes/hostname?is_deleted=false&per_page=1000") or []

    def create_route(self, hostname: str, tunnel_id: str, comment: str) -> dict:
        return self._cf("POST", f"{self.acc}/zerotrust/routes/hostname",
                        {"hostname": hostname, "tunnel_id": tunnel_id, "comment": comment})

    def delete_route(self, route_id: str) -> None:
        try:
            self._cf("DELETE", f"{self.acc}/zerotrust/routes/hostname/{route_id}")
        except HttpError as e:
            if e.status != 404:
                raise

    # DNS — само под *.lan.mcpworks.net; проверката е във всеки метод, не у викащия
    def dns_find(self, name: str) -> list[dict]:
        name = check_lan_name(name)
        q = urllib.parse.urlencode({"type": "A", "name": name})
        return [r for r in (self._cf("GET", f"{self.zone}/dns_records?{q}") or []) if r.get("name") == name]

    def dns_upsert_a(self, name: str, ip: str, comment: str) -> tuple[dict, Optional[str]]:
        """(записът, предишният адрес или None, ако е нов)."""
        name = check_lan_name(name)
        body = {"type": "A", "name": name, "content": ip, "ttl": 300, "proxied": False, "comment": comment}
        cur = self.dns_find(name)
        if cur:
            rec = self._cf("PUT", f"{self.zone}/dns_records/{cur[0]['id']}", body)
            return rec, cur[0].get("content", "")
        return self._cf("POST", f"{self.zone}/dns_records", body), None

    def dns_delete(self, name: str) -> int:
        name = check_lan_name(name)
        n = 0
        for r in self.dns_find(name):
            try:
                self._cf("DELETE", f"{self.zone}/dns_records/{r['id']}")
            except HttpError as e:
                if e.status != 404:
                    raise
            n += 1
        return n


class DryRunCloudflare(Cloudflare):
    """ENR_DRY_RUN=1: същият интерфейс, в паметта, с фалшиви id-та; към CF не излиза нищо."""

    def __init__(self):  # noqa: D107 — без токен, без адрес
        self.tunnels: dict[str, dict] = {}
        self.route_list: list[dict] = []
        self.dns: dict[str, dict] = {}

    def tunnel_by_name(self, name):
        return next((t for t in self.tunnels.values() if t["name"] == name), None)

    def create_tunnel(self, name):
        t = {"id": "dry-" + str(uuid.uuid4()), "name": name}
        self.tunnels[t["id"]] = t
        return t

    def tunnel_token(self, tunnel_id):
        return "dry-token-" + tunnel_id

    def delete_tunnel(self, tunnel_id):
        self.tunnels.pop(tunnel_id, None)

    def routes(self):
        return list(self.route_list)

    def create_route(self, hostname, tunnel_id, comment):
        r = {"id": "dry-" + str(uuid.uuid4()), "hostname": hostname, "tunnel_id": tunnel_id}
        self.route_list.append(r)
        return r

    def delete_route(self, route_id):
        self.route_list = [r for r in self.route_list if r["id"] != route_id]

    def dns_find(self, name):
        name = check_lan_name(name)
        return [self.dns[name]] if name in self.dns else []

    def dns_upsert_a(self, name, ip, comment):
        name = check_lan_name(name)
        prev = self.dns.get(name, {}).get("content")
        self.dns[name] = {"id": "dry-" + name, "name": name, "content": ip}
        return self.dns[name], prev

    def dns_delete(self, name):
        name = check_lan_name(name)
        return 1 if self.dns.pop(name, None) else 0


# ─── цикълът ───────────────────────────────────────────────────────────

class Enroller:
    def __init__(self, ls: LicenseServer, cf: Cloudflare, ci: CertIssuer,
                 cert_url: str, ssh_keys: tuple[str, ...] = ()):
        self.ls, self.cf, self.ci = ls, cf, ci
        self.cert_url = cert_url
        self.ssh_keys = tuple(k.strip() for k in ssh_keys if k.strip())

    def run_once(self) -> dict:
        """Един проход; провал на една каса не спира другите. Връща брой ok/провалени."""
        stats = {"ok": 0, "failed": 0}
        for item in self.ls.pull():
            try:
                ok = self.handle(item)
            except Exception as e:  # noqa: BLE001 — и `done` може да падне; следващият pull опитва пак
                log.error("%s %s: %s", item.get("action"), item.get("till_ref"), e)
                ok = False
            stats["ok" if ok else "failed"] += 1
        return stats

    def handle(self, item: dict) -> bool:
        ref, action = str(item.get("till_ref") or ""), item.get("action")
        client, till = str(item.get("client_slug") or ""), str(item.get("till") or "")
        if not ref:
            log.error("pull: a record without till_ref, skipped")
            return False
        if action not in ("create", "lan", "retire"):
            return self._done(ref, str(action), False, "unknown action")
        if not (SLUG_RE.match(client) and SLUG_RE.match(till)):
            return self._done(ref, action, False, "bad client_slug/till slug")
        return getattr(self, action)(ref, client, till, item)

    def _done(self, ref: str, action: str, ok: bool, note: str) -> bool:
        log.info("%s %s: %s — %s", action, ref, "ok" if ok else "FAILED", note)
        self.ls.done(ref, action, ok, note)
        return ok

    def _rollback(self, undo: list[tuple[str, Callable[[], object]]]) -> list[str]:
        failed = []
        for what, fn in reversed(undo):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                failed.append(f"{what} ({e})")
        return failed

    def _sweep(self, client: str, till: str) -> list[str]:
        """Маха по списъка в CF маршрута и тунела на тази каса (и каквото rollback-ът не е знаел,
        напр. тунел, създаден в CF, но с изгубен отговор). Грешките — като бележки."""
        errors = []
        host = cf_hostname(client, till)
        t = self.cf.tunnel_by_name(tunnel_name(client, till))
        for r in self.cf.routes():
            if r.get("hostname") == host or (t and r.get("tunnel_id") == t["id"]):
                try:
                    self.cf.delete_route(r.get("id") or r.get("hostname_route_id"))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"route ({e})")
        if t:
            try:
                self.cf.delete_tunnel(t["id"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"tunnel ({e})")
        return errors

    def _leftovers(self, client: str, till: str, dns: bool = False) -> list[str]:
        """Какво от тази каса още има в CF — сверено със списъците, не с паметта."""
        left = []
        t = self.cf.tunnel_by_name(tunnel_name(client, till))
        if t:
            left.append(f"tunnel {t['id']}")
        host = cf_hostname(client, till)
        left += [f"route {r.get('hostname')}" for r in self.cf.routes()
                 if r.get("hostname") == host or (t and r.get("tunnel_id") == t["id"])]
        if dns and self.cf.dns_find(lan_name(client, till)):
            left.append(f"dns {lan_name(client, till)}")
        return left

    # ─── create ──
    def create(self, ref: str, client: str, till: str, item: dict) -> bool:
        tname, host = tunnel_name(client, till), cf_hostname(client, till)
        try:
            box_pub = b64u_dec(str(item.get("box_pub") or ""))
        except ValueError:
            box_pub = b""
        if len(box_pub) != 32:
            return self._done(ref, "create", False, "bad box_pub")
        # Заварен тунел или маршрут не е наш: може да е жива каса от лаптопа (kasi.py, ADR-0014
        # осиновяване). Не го ползваме и не го трием — човек решава.
        if self.cf.tunnel_by_name(tname):
            return self._done(ref, "create", False, f"tunnel {tname} already exists; adopt or retire it by hand")
        if any(r.get("hostname") == host for r in self.cf.routes()):
            return self._done(ref, "create", False, f"route {host} already exists; adopt or retire it by hand")

        undo: list[tuple[str, Callable[[], object]]] = []
        step = "tunnel"
        try:
            t = self.cf.create_tunnel(tname)
            undo.append(("tunnel", lambda: self.cf.delete_tunnel(t["id"])))
            step = "route"
            r = self.cf.create_route(host, t["id"], f"till-enroller {ref}")
            undo.append(("route", lambda: self.cf.delete_route(r.get("id") or r.get("hostname_route_id"))))
            step = "token"
            tok = self.cf.tunnel_token(t["id"])
            REDACT.add(tok)
            step = "cert-issuer"
            undo.append(("cert-issuer", lambda: self.ci.remove(host)))
            crt = self.ci.add(host)
            step = "report"
            grant_token, reg = self.ls.report(ref, t["id"], host)
            REDACT.add(grant_token)
            step = "seal"
            efp1 = build_efp1(client, till, host, t["id"], tok, reg, grant_token, self.cert_url,
                              list(self.ssh_keys), crt=crt)
            sealed = seal(box_pub, canonical_json(efp1).encode())
            step = "store"
            self.ls.store(ref, sealed)
        except Exception as e:  # noqa: BLE001
            failed = self._rollback(undo)
            # Преди началото в CF нямаше нищо с тези имена (проверката горе), значи всичко
            # с тях сега е от този проход — мете се по списъка, после се сверява.
            failed += self._sweep(client, till)
            left = self._leftovers(client, till)
            note = f"failed at {step}: {e}; rollback " + (
                "clean (checked in CF)" if not left
                else "LEFT: " + ", ".join(failed + left))
            return self._done(ref, "create", False, note)
        return self._done(ref, "create", True, f"tunnel {t['id']}, route {host}, cert-issuer till, EFP1 stored")

    # ─── lan ──
    def lan(self, ref: str, client: str, till: str, item: dict) -> bool:
        try:
            ip = check_lan_ip(str(item.get("lan_ip") or ""))
            name = lan_name(client, till)
        except ValueError as e:
            return self._done(ref, "lan", False, str(e))
        undo: list[tuple[str, Callable[[], object]]] = []
        step = "dns"
        try:
            rec, prev = self.cf.dns_upsert_a(name, ip, f"till-enroller {ref}")
            if prev is None:
                undo.append(("dns", lambda: self.cf.dns_delete(name)))
            elif prev != ip:
                undo.append(("dns", lambda: self.cf.dns_upsert_a(name, prev, f"till-enroller {ref}")))
            step = "cert-issuer san"
            self.ci.add_san(cf_hostname(client, till))
        except Exception as e:  # noqa: BLE001
            failed = self._rollback(undo)
            return self._done(ref, "lan", False, f"failed at {step}: {e}; rollback "
                              + ("clean" if not failed else "LEFT: " + ", ".join(failed)))
        return self._done(ref, "lan", True, f"A {name} -> {ip}" + (f" (was {prev})" if prev else ""))

    # ─── retire ──
    def retire(self, ref: str, client: str, till: str, item: dict) -> bool:
        host = cf_hostname(client, till)
        t = self.cf.tunnel_by_name(tunnel_name(client, till))
        errors = self._sweep(client, till)
        try:
            self.cf.dns_delete(lan_name(client, till))
        except Exception as e:  # noqa: BLE001
            errors.append(f"dns ({e})")
        try:
            self.ci.remove(host)
        except Exception as e:  # noqa: BLE001
            errors.append(f"cert-issuer ({e})")
        left = self._leftovers(client, till, dns=True)
        if errors or left:
            return self._done(ref, "retire", False, "LEFT: " + ", ".join(errors + left))
        return self._done(ref, "retire", True, f"tunnel {t['id'] if t else '(none)'}, route, DNS, cert-issuer till removed; checked in CF")


# ─── пускане ───────────────────────────────────────────────────────────

REQUIRED = ("LS_URL", "LS_API_KEY", "CI_URL", "CI_ACCESS_CLIENT_ID", "CI_ACCESS_CLIENT_SECRET")
REQUIRED_CF = ("CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_ZONE_ID")


def build(env: Mapping[str, str], cf_api: str = CF_API) -> Enroller:
    dry = env.get("ENR_DRY_RUN") == "1"
    missing = [k for k in REQUIRED + (() if dry else REQUIRED_CF) if not env.get(k)]
    if missing:
        raise SystemExit("till-enroller: missing env " + ", ".join(missing))
    for k in ("CF_API_TOKEN", "LS_API_KEY", "CI_ACCESS_CLIENT_SECRET", "CI_ACCESS_CLIENT_ID"):
        REDACT.add(env.get(k))
    cf = DryRunCloudflare() if dry else Cloudflare(env["CF_API_TOKEN"], env["CF_ACCOUNT_ID"], env["CF_ZONE_ID"], cf_api)
    return Enroller(LicenseServer(env["LS_URL"], env["LS_API_KEY"]),
                    cf,
                    CertIssuer(env["CI_URL"], env["CI_ACCESS_CLIENT_ID"], env["CI_ACCESS_CLIENT_SECRET"]),
                    env.get("CI_PULL_URL") or env["CI_URL"],
                    tuple(env.get("ENR_SSH_PUBKEYS", "").splitlines()))


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(REDACT)
    enr = build(os.environ)
    dry = os.environ.get("ENR_DRY_RUN") == "1"
    if argv and argv[0] == "once":
        log.info("one pass%s: %s", " (dry run)" if dry else "", enr.run_once())
        return 0
    every = int(os.environ.get("ENR_INTERVAL_S", "30"))
    alive = os.environ.get("ENR_ALIVE_FILE", "/tmp/till-enroller.alive")
    log.info("every %ds%s", every, ", DRY RUN — no Cloudflare calls" if dry else "")
    while True:
        try:
            stats = enr.run_once()
            if stats["ok"] or stats["failed"]:
                log.info("pass: %s", stats)
        except Exception as e:  # noqa: BLE001 — LS недостъпен и т.н.; следващият проход опитва пак
            log.error("pass failed: %s", e)
        try:
            with open(alive, "w") as fh:
                fh.write(str(int(time.time())))
        except OSError:
            pass
        time.sleep(every)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
