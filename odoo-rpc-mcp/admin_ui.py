"""
admin_ui.py — Minimal admin web console for the MCP server.

Design goals (from Rosen 2026-04-19):
- Hidden admin portal — path configurable via MCP_ADMIN_PATH_PREFIX, default /admin.
- First-time user logs in with Odoo creds → creates /data/users/<login>/
  with .mcp_auth.json. Subsequent logins use MCP credentials only.
- Admin designated by MCP_BOOTSTRAP_ADMIN env var. First matching login
  gets admin=true automatically.
- No roles (Odoo holds all ACL). Admin can create users, generate
  first-time API keys, and configure per-user Odoo connections.
- Sessions: 24h for regular users, 7 days for admin (per-session cookie
  signed with MCP_SECRET_TOKEN).
- Defenses: Argon2id password hashing, CSRF tokens, rate limiting,
  account lockout, HTTP security headers, optional knock token,
  tarpit delay, audit log.
- 3.3.8 — втори фактор (TOTP, RFC 6238) за конзолата: записване от
  страницата „Сигурност“ (парола → QR → потвърждаващ код → 8 еднократни
  кода за възстановяване), проверка при ВСЯКО издаване на сесия (MCP парола,
  Odoo re-auth, еднократен API key), политика MCP_ADMIN_REQUIRE_TOTP
  (admins|all), нулиране от админ. Тайната е във /data/users/<login>/totp.json
  — същият файл и криптиране (Fernet през MCP_KEY_PEPPER) като MCP
  name-identify в server.py; математиката е в totp_core.py.
- 3.3.8 — ревизия: еднократният API key вече изтича; сесия с незавършен
  setup не стига до API-тата; конзолата отказва да се качи с подразбиращия
  се session secret; DATA_DIR се чете от env; смяна на парола и преглед на
  сесиите вместо празния бутон; HTML escape на потребителските данни.

Integrated into server.py via get_asgi_app() (mounted under the prefix).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError
    _ARGON2_AVAILABLE = True
    _ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
except ImportError:
    _ARGON2_AVAILABLE = False
    _ph = None

try:
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    _ITSDANGEROUS_AVAILABLE = True
except ImportError:
    _ITSDANGEROUS_AVAILABLE = False

try:
    import secrets_registry          # Fernet през MCP_KEY_PEPPER — същото като server.py
    _SECRETS_OK = True
except ImportError:
    secrets_registry = None
    _SECRETS_OK = False

import totp_core                     # RFC 6238 математиката, споделена със server.py

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route

_logger = logging.getLogger("odoo-rpc-mcp.admin_ui")

# ─── Config (env) ────────────────────────────────────────────
ADMIN_PATH_PREFIX = (os.environ.get("MCP_ADMIN_PATH_PREFIX") or "/admin").rstrip("/")
ADMIN_ENABLED = ADMIN_PATH_PREFIX != ""
BOOTSTRAP_ADMIN = (os.environ.get("MCP_BOOTSTRAP_ADMIN") or "").strip().lower()
KNOCK_TOKEN = (os.environ.get("MCP_ADMIN_KNOCK_TOKEN") or "").strip()
_INSECURE_SECRET = "INSECURE-DEFAULT-SET-MCP_SECRET_TOKEN"
SESSION_SECRET = (
    os.environ.get("MCP_ADMIN_SESSION_SECRET")
    or os.environ.get("MCP_SECRET_TOKEN")
    or _INSECURE_SECRET
)
ALLOWED_IPS = [ip.strip() for ip in (os.environ.get("MCP_ADMIN_ALLOWED_IPS") or "").split(",") if ip.strip()]

# Durations
SESSION_TTL_USER = 24 * 3600          # 24h
SESSION_TTL_ADMIN = 7 * 24 * 3600     # 7 days
SETUP_TTL_USER = 24 * 3600            # new user setup window
SETUP_TTL_ADMIN = 7 * 24 * 3600

# Rate limit / lockout
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = 15 * 60                # 15 min
LOCKOUT_STEPS = [15 * 60, 60 * 60, 4 * 3600, 24 * 3600]   # escalating

# Paths — DATA_DIR се чете от env като в server.py (3.3.8). Дотогава беше
# зашит на /data и двете подсистеми можеха да пишат в различни корени.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
USERS_DIR = os.path.join(DATA_DIR, "users")
SESSIONS_DB = os.path.join(DATA_DIR, "sessions.db")
AUDIT_LOG = os.path.join(DATA_DIR, "admin_audit.log")
ADMIN_CONFIG = os.path.join(DATA_DIR, "admin_config.json")

# Втори фактор (3.3.8)
REQUIRE_TOTP = (os.environ.get("MCP_ADMIN_REQUIRE_TOTP") or "").strip().lower()  # "" | "admins" | "all"
PREAUTH_TTL = 5 * 60                  # прозорецът между паролата и кода
TOTP_MAX_FAILS = 5                    # поредни грешни кода на профил…
TOTP_LOCKOUT_S = 300                  # …и колко секунди го заключват
RECOVERY_CODES_N = 8
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # без 0/o, 1/l/i
_RECOVERY_LEN = 10

_SIGNER = URLSafeTimedSerializer(SESSION_SECRET, salt="mcp-admin-v1") if _ITSDANGEROUS_AVAILABLE else None
_PREAUTH_SIGNER = URLSafeTimedSerializer(SESSION_SECRET, salt="mcp-admin-preauth-v1") if _ITSDANGEROUS_AVAILABLE else None


# ─── DB init ─────────────────────────────────────────────────
def _db():
    conn = sqlite3.connect(SESSIONS_DB, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_sessions (
            sid TEXT PRIMARY KEY,
            login TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            csrf_token TEXT NOT NULL,
            ip TEXT,
            ua TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            ip TEXT NOT NULL,
            login TEXT,
            ts INTEGER NOT NULL,
            success INTEGER NOT NULL DEFAULT 0
        )
    """)
    # 3.3.8: чакащ втори фактор — паролата е минала, кодът още не. Редът е
    # еднократен и живее PREAUTH_TTL секунди; intent носи какво да се
    # довърши след кода (dashboard / setup след Odoo re-auth / redeem на key).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_preauth (
            token TEXT PRIMARY KEY,
            login TEXT NOT NULL,
            intent TEXT NOT NULL,
            ip TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_ip_ts ON admin_login_attempts(ip, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON admin_sessions(expires_at)")
    return conn


def _init():
    os.makedirs(USERS_DIR, exist_ok=True)
    Path(AUDIT_LOG).touch(exist_ok=True)
    _db().close()


_init()


# ─── Helpers ─────────────────────────────────────────────────
def _now() -> int:
    return int(time.time())


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_login(login: str) -> str:
    """Make Odoo login safe for filesystem use."""
    safe = "".join(c if c.isalnum() or c in "._-@" else "_" for c in (login or ""))
    return safe.lower()[:120]


def _user_auth_path(login: str) -> str:
    return os.path.join(USERS_DIR, _sanitize_login(login), ".mcp_auth.json")


def _user_dir(login: str) -> str:
    return os.path.join(USERS_DIR, _sanitize_login(login))


def _load_user_auth(login: str) -> dict | None:
    p = _user_auth_path(login)
    if not os.path.isfile(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_user_auth(login: str, data: dict):
    d = _user_dir(login)
    os.makedirs(d, exist_ok=True)
    p = _user_auth_path(login)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def _list_users() -> list[str]:
    if not os.path.isdir(USERS_DIR):
        return []
    out = []
    for name in sorted(os.listdir(USERS_DIR)):
        if os.path.isfile(os.path.join(USERS_DIR, name, ".mcp_auth.json")):
            out.append(name)
    return out


def _hash_password(pw: str) -> str:
    if _ARGON2_AVAILABLE:
        return _ph.hash(pw)
    # Fallback: scrypt (stdlib). Weaker than argon2 but still strong.
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + dk.hex()


def _verify_password(pw: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        if hashed.startswith("scrypt$"):
            _, salt_h, dk_h = hashed.split("$")
            dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_h), n=2**14, r=8, p=1, dklen=32)
            return hmac.compare_digest(dk.hex(), dk_h)
        if _ARGON2_AVAILABLE:
            return _ph.verify(hashed, pw)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False
    return False


def _gen_api_key() -> str:
    """Same format as MCP_SECRET_TOKEN — 43-char base64url."""
    return secrets.token_urlsafe(32)


def _hash_api_key(key: str) -> str:
    """HMAC-SHA256 of the API key, keyed with session secret. Deterministic (for lookup)."""
    return hmac.new(SESSION_SECRET.encode(), key.encode(), hashlib.sha256).hexdigest()


def _audit(actor: str, action: str, target: str = "", ip: str = "", ua: str = "", extra: dict | None = None):
    entry = {
        "ts": _iso_now(),
        "actor": actor,
        "action": action,
        "target": target,
        "ip": ip,
        "ua": (ua or "")[:200],
    }
    if extra:
        entry.update(extra)
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        _logger.warning("audit write failed: %s", e)


def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    host = (req.client.host if req.client else "") or "unknown"
    return host


def _is_ip_allowed(ip: str) -> bool:
    if not ALLOWED_IPS:
        return True
    try:
        ip_addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in ALLOWED_IPS:
        try:
            if "/" in entry:
                if ip_addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip_addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def _check_knock(req: Request) -> bool:
    if not KNOCK_TOKEN:
        return True
    t = req.query_params.get("k") or req.headers.get("x-knock") or ""
    return hmac.compare_digest(t, KNOCK_TOKEN)


# ─── Rate limiting & lockout ─────────────────────────────────
def _record_attempt(ip: str, login: str | None, success: bool):
    with _db() as conn:
        conn.execute(
            "INSERT INTO admin_login_attempts(ip, login, ts, success) VALUES (?,?,?,?)",
            (ip, login or "", _now(), 1 if success else 0),
        )
        # cleanup old
        conn.execute("DELETE FROM admin_login_attempts WHERE ts < ?", (_now() - 86400,))


def _recent_failures(ip: str, login: str | None = None) -> tuple[int, int]:
    """Return (count_in_window, seconds_since_last_fail). login=None → ip-only check."""
    cutoff = _now() - LOGIN_WINDOW
    with _db() as conn:
        if login:
            row = conn.execute(
                "SELECT COUNT(*) AS c, MAX(ts) AS last FROM admin_login_attempts "
                "WHERE login=? AND success=0 AND ts >= ?",
                (login, cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c, MAX(ts) AS last FROM admin_login_attempts "
                "WHERE ip=? AND success=0 AND ts >= ?",
                (ip, cutoff),
            ).fetchone()
    c = row["c"] or 0
    last = row["last"] or 0
    return c, (_now() - last) if last else 0


def _lockout_remaining(ip: str, login: str | None = None) -> int:
    """Seconds until lockout expires. 0 = not locked."""
    c, _ = _recent_failures(ip, login)
    if c < LOGIN_MAX_ATTEMPTS:
        return 0
    # Compute escalating lockout
    step = min((c - LOGIN_MAX_ATTEMPTS), len(LOCKOUT_STEPS) - 1)
    duration = LOCKOUT_STEPS[step]
    # Find last fail ts
    with _db() as conn:
        row = conn.execute(
            "SELECT MAX(ts) AS last FROM admin_login_attempts WHERE %s=? AND success=0"
            % ("login" if login else "ip"),
            (login if login else ip,),
        ).fetchone()
    last = (row["last"] if row else 0) or 0
    remaining = (last + duration) - _now()
    return max(0, remaining)


async def _tarpit_delay(failures: int):
    """Sleep exponential: 1, 2, 4, 8... seconds before responding on failed login."""
    if failures <= 0:
        return
    delay = min(2 ** min(failures - 1, 5), 32)
    await asyncio.sleep(delay)


# ─── Session management ──────────────────────────────────────
def _create_session(login: str, is_admin: bool, ip: str, ua: str) -> tuple[str, str]:
    """Return (sid, csrf_token)."""
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    ttl = SESSION_TTL_ADMIN if is_admin else SESSION_TTL_USER
    with _db() as conn:
        conn.execute(
            "INSERT INTO admin_sessions(sid, login, is_admin, csrf_token, ip, ua, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, login, 1 if is_admin else 0, csrf, ip, (ua or "")[:200], _now(), _now() + ttl),
        )
    return sid, csrf


def _get_session(sid: str) -> dict | None:
    if not sid:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT sid, login, is_admin, csrf_token, expires_at FROM admin_sessions WHERE sid=?",
            (sid,),
        ).fetchone()
    if not row:
        return None
    if row["expires_at"] < _now():
        _delete_session(sid)
        return None
    return dict(row)


def _delete_session(sid: str):
    with _db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE sid=?", (sid,))


def _sid_cookie_name() -> str:
    return "mcp_admin_session"


def _set_session_cookie(resp: Response, sid: str, is_admin: bool):
    ttl = SESSION_TTL_ADMIN if is_admin else SESSION_TTL_USER
    signed = _SIGNER.dumps(sid) if _SIGNER else sid
    resp.set_cookie(
        _sid_cookie_name(),
        signed,
        max_age=ttl,
        httponly=True,
        secure=True,
        samesite="lax",
        path=ADMIN_PATH_PREFIX or "/",
    )


def _clear_session_cookie(resp: Response):
    resp.delete_cookie(_sid_cookie_name(), path=ADMIN_PATH_PREFIX or "/")


def _read_session(req: Request) -> dict | None:
    cookie = req.cookies.get(_sid_cookie_name())
    if not cookie:
        return None
    if _SIGNER:
        try:
            sid = _SIGNER.loads(cookie, max_age=SESSION_TTL_ADMIN)
        except (BadSignature, SignatureExpired):
            return None
    else:
        sid = cookie
    return _get_session(sid)


# ─── Pre-auth: между паролата и втория фактор (3.3.8) ────────
def _preauth_create(login: str, intent: dict, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    with _db() as conn:
        conn.execute("DELETE FROM admin_preauth WHERE expires_at < ?", (_now(),))
        conn.execute(
            "INSERT INTO admin_preauth(token, login, intent, ip, created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (token, login, json.dumps(intent), ip, _now(), _now() + PREAUTH_TTL),
        )
    return token


def _preauth_get(token: str) -> dict | None:
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT token, login, intent, ip, expires_at FROM admin_preauth WHERE token=?", (token,)
        ).fetchone()
    if not row:
        return None
    if row["expires_at"] < _now():
        _preauth_delete(token)
        return None
    out = dict(row)
    try:
        out["intent"] = json.loads(out.get("intent") or "{}")
    except json.JSONDecodeError:
        out["intent"] = {}
    return out


def _preauth_delete(token: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM admin_preauth WHERE token=?", (token,))


def _preauth_cookie_name() -> str:
    return "mcp_admin_preauth"


def _set_preauth_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        _preauth_cookie_name(),
        _PREAUTH_SIGNER.dumps(token) if _PREAUTH_SIGNER else token,
        max_age=PREAUTH_TTL, httponly=True, secure=True, samesite="lax",
        path=ADMIN_PATH_PREFIX or "/",
    )


def _clear_preauth_cookie(resp: Response) -> None:
    resp.delete_cookie(_preauth_cookie_name(), path=ADMIN_PATH_PREFIX or "/")


def _read_preauth(req: Request) -> dict | None:
    cookie = req.cookies.get(_preauth_cookie_name())
    if not cookie:
        return None
    if _PREAUTH_SIGNER:
        try:
            token = _PREAUTH_SIGNER.loads(cookie, max_age=PREAUTH_TTL)
        except (BadSignature, SignatureExpired):
            return None
    else:
        token = cookie
    return _preauth_get(token)


# ─── Преглед и отнемане на сесии (3.3.8) ─────────────────────
def _list_sessions(login: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT sid, ip, ua, is_admin, created_at, expires_at FROM admin_sessions "
            "WHERE login=? AND expires_at >= ? ORDER BY created_at DESC", (login, _now()),
        ).fetchall()
    return [dict(r) for r in rows]


def _delete_other_sessions(login: str, keep_sid: str) -> int:
    with _db() as conn:
        cur = conn.execute("DELETE FROM admin_sessions WHERE login=? AND sid<>?", (login, keep_sid))
        return cur.rowcount or 0


# ─── Odoo validation ─────────────────────────────────────────
class _UATransport(xmlrpc.client.Transport):
    """xmlrpc Transport with custom User-Agent — Cloudflare Bot Fight Mode blocks
    the default 'Python-xmlrpc/3.x' UA (returns 403 before authenticate() runs)."""
    user_agent = "OdooMcpAdmin/1.0 (+https://mcp.odoo-shell.space)"


class _UASafeTransport(xmlrpc.client.SafeTransport):
    """HTTPS version of _UATransport."""
    user_agent = "OdooMcpAdmin/1.0 (+https://mcp.odoo-shell.space)"

    def __init__(self, context=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ctx = context

    def make_connection(self, host):
        # Use our SSL context (disables verify for TOFU on self-signed)
        import http.client
        chost, self._extra_headers, _x509 = self.get_host_info(host)
        self._connection = host, http.client.HTTPSConnection(
            chost, None, context=self._ctx
        )
        return self._connection[1]


def _validate_odoo(url: str, db: str, login: str, password_or_key: str) -> int | None:
    """Return uid if auth ok, None otherwise.

    Uses custom User-Agent transport to avoid Cloudflare Bot Fight Mode
    blocking default 'Python-xmlrpc' UA (returns 403 before authenticate runs).
    SSL verification is disabled to support self-signed certs (TOFU is handled
    in parent MCP connection flow, but admin UI is first-contact and doesn't
    have cert pinning yet)."""
    try:
        url = (url or "").rstrip("/")
        if not url or not db or not login or not password_or_key:
            return None
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        if url.startswith("https://"):
            transport = _UASafeTransport(context=ctx)
        else:
            transport = _UATransport()
        proxy = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common",
            allow_none=True,
            transport=transport,
        )
        uid = proxy.authenticate(db, login, password_or_key, {})
        return int(uid) if uid else None
    except Exception as e:
        _logger.warning("Odoo auth failed (url=%s db=%s login=%s): %s", url, db, login, e)
        return None


# ─── Втори фактор: съхранение, проверка, политика (3.3.8) ────
# Същият файл и схема като MCP name-identify (server.py, 3.3.0):
#   /data/users/<login>/totp.json → secret_enc (Fernet през MCP_KEY_PEPPER),
#   algo / digits / step / last_step / last_used.
# Конзолата добавя: pending_secret_enc (записване, което още не е потвърдено
# с код) и recovery {hashes, generated_at} — HMAC (през същия pepper) на
# еднократните кодове за възстановяване.
# Директорията на конзолата пази „@“ в името, а MCP принципалът го маха ⇒ при
# имейл-логин двата профила са различни и факторите не се смесват. Съвпадат
# само когато логинът и принципалът са буквално една и съща дума — тогава една
# тайна служи и за двете, което е желаното (един човек, един фактор).
_totp_attempts: dict = {}            # login -> {"fails": int, "until": float}
_totp_lock = threading.Lock()


def _totp_path(login: str) -> str:
    return os.path.join(_user_dir(login), "totp.json")


def _totp_load(login: str) -> dict | None:
    p = _totp_path(login)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _totp_save(login: str, data: dict) -> None:
    os.makedirs(_user_dir(login), exist_ok=True)
    p = _totp_path(login)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _totp_remove(login: str) -> None:
    try:
        os.remove(_totp_path(login))
    except OSError:
        pass
    with _totp_lock:
        _totp_attempts.pop(login, None)


def _totp_enrolled(login: str) -> bool:
    d = _totp_load(login)
    return bool(d and d.get("secret_enc"))


def _totp_pepper_ok() -> bool:
    return bool(_SECRETS_OK and secrets_registry._pepper())


def _totp_public(login: str) -> dict:
    """Състоянието без тайни — за страницата „Сигурност“ и за списъка на админа."""
    d = _totp_load(login) or {}
    rec = d.get("recovery") or {}
    return {
        "enrolled": bool(d.get("secret_enc")),
        "pending": bool(d.get("pending_secret_enc")),
        "enrolled_at": d.get("enrolled_at"),
        "last_used": d.get("last_used"),
        "recovery_left": len(rec.get("hashes") or []),
        "pepper_ready": _totp_pepper_ok(),
    }


def _totp_begin_enroll(login: str) -> dict:
    """Нова тайна в pending. Активната (ако има) остава валидна, докато кодът
    не потвърди новата — така QR, който човекът не е сканирал, не го заключва."""
    if not _totp_pepper_ok():
        return {"error": "MCP_KEY_PEPPER не е зададен (≥32 знака) — тайната няма с какво да се криптира."}
    secret = totp_core.secret_new()
    enc = secrets_registry._encrypt(secret)
    if not enc:
        return {"error": "Криптирането не сработи (cryptography/Fernet липсва?)."}
    d = _totp_load(login) or {}
    d["pending_secret_enc"] = enc
    d["pending_at"] = _iso_now()
    _totp_save(login, d)
    uri = totp_core.provisioning_uri(f"OdooMCP:{login}", secret)
    out = {"ok": True, "secret": secret, "otpauth_uri": uri}
    out.update(totp_core.qr(uri))
    return out


def _recovery_hash(code: str) -> str:
    norm = re.sub(r"[^a-z0-9]", "", (code or "").lower())
    return secrets_registry._hmac_hex("admin-recovery:" + norm) if norm and _SECRETS_OK else ""


def _recovery_new(login: str) -> list[str]:
    """Осем еднократни кода; пазят се само HMAC-овете им (през pepper-а).
    Показват се веднъж — както API key-ът."""
    codes = ["".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LEN))
             for _ in range(RECOVERY_CODES_N)]
    d = _totp_load(login) or {}
    d["recovery"] = {"hashes": [_recovery_hash(c) for c in codes], "generated_at": _iso_now()}
    _totp_save(login, d)
    return [c[:5] + "-" + c[5:] for c in codes]


def _recovery_check(login: str, code: str) -> bool:
    d = _totp_load(login)
    hashes = list(((d or {}).get("recovery") or {}).get("hashes") or [])
    h = _recovery_hash(code)
    if not d or not hashes or not h:
        return False
    for i, stored in enumerate(hashes):
        if hmac.compare_digest(h, stored):
            hashes.pop(i)                      # еднократен
            d["recovery"]["hashes"] = hashes
            d["recovery"]["last_used"] = _iso_now()
            _totp_save(login, d)
            return True
    return False


def _totp_confirm_enroll(login: str, code: str) -> dict:
    """Кодът от приложението доказва, че тайната е стигнала до човека — едва
    тогава pending става активна и се раждат кодовете за възстановяване."""
    d = _totp_load(login) or {}
    enc = d.get("pending_secret_enc")
    if not enc:
        return {"error": "Няма започнато записване."}
    secret = secrets_registry._decrypt(enc) if _totp_pepper_ok() else None
    if not secret:
        return {"error": "Тайната не се дешифрира — сменен MCP_KEY_PEPPER? Започнете отначало."}
    ok, step = totp_core.verify(secret, code)
    if not ok:
        return {"error": "Кодът не съвпада. Проверете часовника на телефона и опитайте пак."}
    _totp_save(login, {
        "secret_enc": enc,
        "enrolled_at": _iso_now(),
        "algo": "SHA1", "digits": totp_core.DIGITS, "step": totp_core.STEP,
        "last_step": int(step),                # потвърждаващият код не влиза повторно
        "last_used": None,
    })
    with _totp_lock:
        _totp_attempts.pop(login, None)
    return {"ok": True, "recovery_codes": _recovery_new(login)}


def _totp_check(login: str, code: str) -> dict:
    """Проверка при вход: заключване след TOTP_MAX_FAILS поредни грешки, replay
    guard по стъпка. Връща {"ok", "reason"?, "retry_after"?}."""
    with _totp_lock:
        st = _totp_attempts.get(login)
        if st and st.get("until", 0) > time.time():
            return {"ok": False, "reason": "locked", "retry_after": int(st["until"] - time.time())}
    d = _totp_load(login)
    if not d or not d.get("secret_enc"):
        return {"ok": False, "reason": "not_enrolled"}
    if not _totp_pepper_ok():
        return {"ok": False, "reason": "weak_or_missing_pepper"}
    secret = secrets_registry._decrypt(d["secret_enc"])
    if not secret:
        return {"ok": False, "reason": "decrypt_failed"}
    ok, step = totp_core.verify(secret, code)
    replay = False
    if ok and d.get("last_step") is not None and step is not None and int(step) <= int(d["last_step"]):
        ok, replay = False, True
    with _totp_lock:
        if ok:
            _totp_attempts.pop(login, None)
        else:
            a = _totp_attempts.get(login, {"fails": 0, "until": 0})
            a["fails"] = a.get("fails", 0) + 1
            if a["fails"] >= TOTP_MAX_FAILS:
                a["until"] = time.time() + TOTP_LOCKOUT_S
                a["fails"] = 0
            _totp_attempts[login] = a
    if ok:
        d["last_step"] = int(step)
        d["last_used"] = _iso_now()
        _totp_save(login, d)
        return {"ok": True}
    return {"ok": False, "reason": "replay" if replay else "invalid_code"}


def _totp_policy_applies(au: dict) -> bool:
    if REQUIRE_TOTP == "all":
        return True
    if REQUIRE_TOTP == "admins":
        return bool(au.get("admin"))
    return False


def _enrollment_required(login: str, au: dict) -> bool:
    return _totp_policy_applies(au) and not _totp_enrolled(login)


def _password_ok(login: str, pw: str) -> bool:
    au = _load_user_auth(login)
    return bool(au and pw and _verify_password(pw, au.get("password_hash", "")))


def _autosave_default_connection(login: str, url: str, db: str, api_key: str, ip: str, ua: str) -> None:
    """Първата проверена Odoo връзка влиза като alias 'default', за да има
    потребителят поне една веднага след setup. Не презаписва съществуващ."""
    try:
        data = _load_connections(login)
        if "default" in data:
            return
        data["default"] = {
            "url": url.rstrip("/"), "db": db, "user": login, "api_key": api_key,
            "protocol": "xmlrpc", "verify_ssl": True,
        }
        _save_connections(login, data)
        _audit(login, "connection_autosave", "default", ip, ua)
    except Exception as _e:  # noqa: BLE001
        _logger.warning("auto-save connection failed: %s", _e)


# ─── Издаване на сесия — единствената врата (3.3.8) ──────────
# Всеки път, който доказва първия фактор (MCP парола, Odoo re-auth, еднократен
# API key), минава оттук. Записан втори фактор ⇒ сесия НЕ се издава; вместо
# това pre-auth ред + бисквитка, а страничните ефекти на intent-а (setup флаг,
# изгаряне на key-а) чакат кода. Иначе Odoo паролата сама би заобиколила TOTP-а
# през „забравена парола“.
def _complete_login(login: str, au: dict, intent: dict, ip: str, ua: str,
                    second_factor: str | None) -> Response:
    kind = intent.get("kind", "dashboard")
    if kind == "setup":
        if intent.get("redeem"):
            au["api_key_hash"] = ""
            _save_user_auth(login, au)
            _audit(login, "setup_api_key_redeemed", "", ip, ua)
        if intent.get("odoo"):
            au["setup_pending"] = True
            au.setdefault("odoo", {}).update(intent["odoo"])
            _save_user_auth(login, au)
            _audit(login, "user_reauth_via_odoo", "", ip, ua)
    is_admin = bool(au.get("admin", False))
    sid, _csrf = _create_session(login, is_admin, ip, ua)
    _audit(login, "login_ok", "", ip, ua, {"admin": is_admin, "second_factor": second_factor})
    nxt = f"{ADMIN_PATH_PREFIX}/setup" if au.get("setup_pending") else f"{ADMIN_PATH_PREFIX}/dashboard"
    resp = JSONResponse({"ok": True, "next": nxt})
    _set_session_cookie(resp, sid, is_admin)
    return _apply_sec_headers(resp)


def _finish_login(login: str, au: dict, intent: dict, ip: str, ua: str) -> Response:
    if _totp_enrolled(login):
        token = _preauth_create(login, intent, ip)
        _audit(login, "login_totp_pending", "", ip, ua, {"intent": intent.get("kind", "dashboard")})
        resp = JSONResponse({"ok": True, "totp_required": True, "next": f"{ADMIN_PATH_PREFIX}/totp"})
        _set_preauth_cookie(resp, token)
        return _apply_sec_headers(resp)
    return _complete_login(login, au, intent, ip, ua, second_factor=None)


# ─── HTTP security headers middleware ────────────────────────
SEC_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "font-src 'self' https://cdn.jsdelivr.net data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


def _apply_sec_headers(resp: Response) -> Response:
    for k, v in SEC_HEADERS.items():
        resp.headers[k] = v
    return resp


# ─── Shared pre-request checks ───────────────────────────────
def _gate(req: Request) -> Response | None:
    """Return a Response to short-circuit (404/429) or None to proceed."""
    if not ADMIN_ENABLED:
        return PlainTextResponse("Not Found", status_code=404)
    ip = _client_ip(req)
    if not _is_ip_allowed(ip):
        _logger.warning("admin: IP not allowed: %s", ip)
        return PlainTextResponse("Not Found", status_code=404)
    if not _check_knock(req):
        return PlainTextResponse("Not Found", status_code=404)
    return None


# ─── HTML templates ──────────────────────────────────────────
BOOTSTRAP_CSS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
BOOTSTRAP_JS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"
BOOTSTRAP_ICONS = "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"

BASE_CSS = """
:root {
  --brand: #714BA0;
  --brand-dark: #4E2F75;
  --accent: #21B6B7;
  --bg: #F6F4FA;
}
body { background: var(--bg); font-family: 'Inter', system-ui, sans-serif; }
.brand { color: var(--brand); font-weight: 800; letter-spacing: -0.02em; }
.btn-brand { background: var(--brand); color: #fff; border: none; }
.btn-brand:hover { background: var(--brand-dark); color: #fff; }
.btn-accent { background: var(--accent); color: #fff; border: none; }
.btn-accent:hover { background: #1a9d9e; color: #fff; }
.btn-outline-accent { border: 1px solid var(--accent); color: var(--accent); background: transparent; }
.btn-outline-accent:hover { background: var(--accent); color: #fff; }
.card.glass { background: rgba(255,255,255,0.95); backdrop-filter: blur(12px); }
.navbar { background: #fff !important; border-bottom: 1px solid rgba(113,75,160,0.12); }
.card-header.brand-bg { background: linear-gradient(135deg, var(--brand) 0%, var(--accent) 100%); color: #fff; }
.text-brand { color: var(--brand); }
.gradient-bg {
  background: linear-gradient(135deg, var(--brand-dark) 0%, var(--brand) 60%, var(--accent) 140%);
  min-height: 100vh;
}
.login-card { max-width: 480px; margin: 0 auto; }
code.apikey {
  display: block; padding: 12px; background: #1A1A2E; color: #4FCACB;
  border-radius: 8px; word-break: break-all; font-size: 0.82em;
}
.table-mono td, .table-mono code { font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 0.88em; }
"""


def _html_shell(title: str, body: str, extra_head: str = "") -> str:
    # Inline SVG favicon — purple shield + "M" (MCP). No external request.
    favicon = (
        "data:image/svg+xml;utf8,"
        "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2064%2064'%3E"
        "%3Crect%20width='64'%20height='64'%20rx='12'%20fill='%23714BA0'/%3E"
        "%3Ctext%20x='32'%20y='46'%20font-family='Inter,sans-serif'%20"
        "font-size='40'%20font-weight='800'%20fill='white'%20"
        "text-anchor='middle'%3EM%3C/text%3E%3C/svg%3E"
    )
    return f"""<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>{title}</title>
<link rel="icon" type="image/svg+xml" href="{favicon}">
<link href="{BOOTSTRAP_CSS}" rel="stylesheet">
<link href="{BOOTSTRAP_ICONS}" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{BASE_CSS}</style>
{extra_head}
</head>
<body>
{body}
<script src="{BOOTSTRAP_JS}"></script>
</body>
</html>
"""


def _nav(sess: dict | None) -> str:
    if not sess:
        return ""
    login = sess["login"]
    is_admin = bool(sess["is_admin"])
    admin_link = f'<li class="nav-item"><a class="nav-link" href="{ADMIN_PATH_PREFIX}/users">Потребители</a></li>' if is_admin else ""
    return f"""
<nav class="navbar navbar-expand-lg sticky-top shadow-sm">
  <div class="container-fluid px-4">
    <a class="navbar-brand brand" href="{ADMIN_PATH_PREFIX}/dashboard">
      <i class="bi bi-shield-lock-fill"></i> MCP Admin
    </a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div class="collapse navbar-collapse" id="nav">
      <ul class="navbar-nav me-auto">
        <li class="nav-item"><a class="nav-link" href="{ADMIN_PATH_PREFIX}/dashboard">Dashboard</a></li>
        <li class="nav-item"><a class="nav-link" href="{ADMIN_PATH_PREFIX}/connections">Odoo връзки</a></li>
        <li class="nav-item"><a class="nav-link" href="{ADMIN_PATH_PREFIX}/security">Сигурност</a></li>
        <li class="nav-item"><a class="nav-link" href="{ADMIN_PATH_PREFIX}/backups">Backups</a></li>
        <li class="nav-item"><a class="nav-link" href="{ADMIN_PATH_PREFIX}/filestore">Filestore</a></li>
        {admin_link}
      </ul>
      <div class="d-flex align-items-center gap-3">
        <span class="text-muted small">
          <i class="bi bi-person-circle"></i> {login}
          {'<span class="badge bg-warning text-dark ms-1">admin</span>' if is_admin else ''}
        </span>
        <a class="btn btn-outline-secondary btn-sm" href="{ADMIN_PATH_PREFIX}/logout">
          <i class="bi bi-box-arrow-right"></i> Изход
        </a>
      </div>
    </div>
  </div>
</nav>
"""


# ─── Handlers ────────────────────────────────────────────────
async def _handle_root(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if sess:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/dashboard", status_code=302)
    return RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)


async def _handle_login_page(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if sess:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/dashboard", status_code=302)

    body = f"""
<div class="gradient-bg d-flex align-items-center py-5">
  <div class="container">
    <div class="text-center text-white mb-4">
      <i class="bi bi-shield-lock-fill" style="font-size:3rem;"></i>
      <h1 class="mt-2 mb-1">MCP Admin Console</h1>
      <p class="opacity-75">Достъп само за упълномощени потребители</p>
    </div>
    <div class="login-card card shadow-lg border-0">
      <div class="card-body p-4 p-md-5">
        <ul class="nav nav-pills nav-fill mb-4" role="tablist">
          <li class="nav-item" role="presentation">
            <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#mcp-tab" type="button">
              <i class="bi bi-key"></i> MCP Login
            </button>
          </li>
          <li class="nav-item" role="presentation">
            <button class="nav-link" data-bs-toggle="tab" data-bs-target="#odoo-tab" type="button">
              <i class="bi bi-door-open"></i> Първи път? Odoo
            </button>
          </li>
        </ul>

        <div class="tab-content">
          <div class="tab-pane fade show active" id="mcp-tab">
            <form id="mcpForm">
              <div class="mb-3">
                <label class="form-label small fw-semibold">Потребителско име (Odoo login)</label>
                <input name="login" type="email" class="form-control" required autocomplete="username">
              </div>
              <div class="mb-3">
                <label class="form-label small fw-semibold">MCP парола</label>
                <input name="password" type="password" class="form-control" required autocomplete="current-password">
              </div>
              <!-- honeypot -->
              <input name="website" type="text" style="position:absolute;left:-9999px;" tabindex="-1" autocomplete="off">
              <button class="btn btn-brand w-100 py-2">Вход</button>
            </form>
          </div>

          <div class="tab-pane fade" id="odoo-tab">
            <p class="small text-muted">Първо влизане или нова регистрация след получен API key от админа.</p>
            <form id="odooForm">
              <div class="row g-2 mb-3">
                <div class="col-12">
                  <label class="form-label small fw-semibold">Odoo URL</label>
                  <input name="url" type="url" class="form-control" placeholder="https://yourcompany.odoo.com" required>
                </div>
                <div class="col-6">
                  <label class="form-label small fw-semibold">Database</label>
                  <input name="db" type="text" class="form-control" required>
                </div>
                <div class="col-6">
                  <label class="form-label small fw-semibold">Login</label>
                  <input name="login" type="email" class="form-control" required>
                </div>
              </div>
              <div class="mb-3">
                <label class="form-label small fw-semibold">Odoo парола ИЛИ API key ИЛИ MCP setup token</label>
                <input name="password" type="password" class="form-control" required>
                <small class="text-muted">Ключа за първа регистрация е издаден от админа или това е вашата Odoo парола.</small>
              </div>
              <input name="website" type="text" style="position:absolute;left:-9999px;" tabindex="-1" autocomplete="off">
              <button class="btn btn-accent w-100 py-2">Валидирай &amp; продължи</button>
            </form>
          </div>
        </div>

        <div id="msg" class="mt-3"></div>
      </div>
    </div>
    <p class="text-center text-white-50 small mt-4">
      <i class="bi bi-info-circle"></i> Забравена парола → свържете се с админа
    </p>
  </div>
</div>

<script>
function setMsg(txt, type) {{
  const m = document.getElementById('msg');
  m.className = 'alert alert-' + (type || 'info') + ' small';
  m.textContent = txt;
}}
function clearMsg() {{ document.getElementById('msg').className = ''; document.getElementById('msg').textContent = ''; }}

document.getElementById('mcpForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  clearMsg();
  const data = Object.fromEntries(new FormData(e.target));
  if (data.website) return;
  setMsg('Проверявам...', 'secondary');
  try {{
    const r = await fetch('{ADMIN_PATH_PREFIX}/api/login/mcp', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(data),
      credentials: 'include',
    }});
    const j = await r.json();
    if (r.ok) {{ window.location.href = j.next || '{ADMIN_PATH_PREFIX}/dashboard'; }}
    else setMsg(j.error || 'Грешка', 'danger');
  }} catch (err) {{ setMsg('Мрежова грешка: ' + err, 'danger'); }}
}});

document.getElementById('odooForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  clearMsg();
  const data = Object.fromEntries(new FormData(e.target));
  if (data.website) return;
  setMsg('Валидирам с Odoo (до 15 секунди)...', 'secondary');
  try {{
    const r = await fetch('{ADMIN_PATH_PREFIX}/api/login/odoo', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(data),
      credentials: 'include',
    }});
    const j = await r.json();
    if (r.ok) {{ window.location.href = j.next || '{ADMIN_PATH_PREFIX}/setup'; }}
    else setMsg(j.error || 'Грешка', 'danger');
  }} catch (err) {{ setMsg('Мрежова грешка: ' + err, 'danger'); }}
}});
</script>
"""
    resp = HTMLResponse(_html_shell("MCP Admin · Вход", body))
    return _apply_sec_headers(resp)


async def _api_login_mcp(req: Request):
    gate = _gate(req)
    if gate: return gate
    ip = _client_ip(req); ua = req.headers.get("user-agent", "")
    try:
        data = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error":"Bad request"}, status_code=400)
    if data.get("website"):
        return Response(status_code=204)   # honeypot hit
    login = _sanitize_login(data.get("login") or "")
    password = data.get("password") or ""
    if not login or not password:
        return JSONResponse({"error":"Въведете login и парола"}, status_code=400)

    # Lockout check — по IP И по профил (3.3.8): зад общ NAT/прокси един
    # нападател заключваше всички, а с редуване на адреси не се заключваше никой.
    rem = max(_lockout_remaining(ip), _lockout_remaining(ip, login))
    if rem > 0:
        return JSONResponse({"error": f"Твърде много опити. Изчакайте {max(1, rem // 60)}м."}, status_code=429)

    au = _load_user_auth(login)
    fail, _ = _recent_failures(ip)
    if not au or not _verify_password(password, au.get("password_hash", "")):
        _record_attempt(ip, login, False)
        await _tarpit_delay(fail + 1)
        _audit(login, "login_fail", "", ip, ua)
        return JSONResponse({"error":"Грешен login или парола"}, status_code=401)

    _record_attempt(ip, login, True)
    return _finish_login(login, au, {"kind": "dashboard"}, ip, ua)


async def _api_login_odoo(req: Request):
    gate = _gate(req)
    if gate: return gate
    ip = _client_ip(req); ua = req.headers.get("user-agent", "")
    try:
        data = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error":"Bad request"}, status_code=400)
    if data.get("website"):
        return Response(status_code=204)
    url = data.get("url") or ""
    db = data.get("db") or ""
    login = _sanitize_login(data.get("login") or "")
    password = data.get("password") or ""
    if not all([url, db, login, password]):
        return JSONResponse({"error":"Всички полета са задължителни"}, status_code=400)

    rem = max(_lockout_remaining(ip), _lockout_remaining(ip, login))
    if rem > 0:
        return JSONResponse({"error": f"Твърде много опити. Изчакайте {max(1, rem // 60)}м."}, status_code=429)

    au = _load_user_auth(login)
    fail, _ = _recent_failures(ip)

    # Case 1: user has pending api_key_hash → password == API key issued by admin
    if au and au.get("setup_pending") and au.get("api_key_hash"):
        expires = int(au.get("api_key_expires") or 0)
        if expires and expires < _now():
            # 3.3.8: срокът се записваше, но никой не го четеше — ключът беше вечен.
            _record_attempt(ip, login, False)
            await _tarpit_delay(fail + 1)
            _audit(login, "setup_api_key_expired", "", ip, ua)
            return JSONResponse({"error":"API key-ят е изтекъл. Поискайте нов от админа."}, status_code=401)
        if hmac.compare_digest(_hash_api_key(password), au["api_key_hash"]):
            _record_attempt(ip, login, True)
            # Изгарянето на key-а е в intent-а: при записан втори фактор чака кода.
            return _finish_login(login, au, {"kind": "setup", "redeem": True}, ip, ua)
        _record_attempt(ip, login, False)
        await _tarpit_delay(fail + 1)
        return JSONResponse({"error":"Невалиден API key"}, status_code=401)

    # Case 2: Odoo validation (new user OR existing user changing password)
    uid = _validate_odoo(url, db, login, password)
    if not uid:
        _record_attempt(ip, login, False)
        await _tarpit_delay(fail + 1)
        _audit(login, "odoo_auth_fail", db, ip, ua, {"url": url})
        return JSONResponse({"error":"Odoo auth неуспешен. Проверете URL, DB, login, парола."}, status_code=401)
    _record_attempt(ip, login, True)

    # Bootstrap admin flag?
    is_admin = (BOOTSTRAP_ADMIN and login == _sanitize_login(BOOTSTRAP_ADMIN))

    if not au:
        # New user — няма как да има втори фактор; сесията се издава веднага.
        au = {
            "login": login,
            "admin": is_admin,
            "created_at": _now(),
            "setup_pending": True,
            "password_hash": "",
            "api_key_hash": "",
            "odoo": {"url": url.rstrip("/"), "db": db, "uid": uid},
        }
        _save_user_auth(login, au)
        _audit(login, "user_created_via_odoo", "", ip, ua, {"admin": is_admin, "db": db})
        _autosave_default_connection(login, url, db, password, ip, ua)
        return _finish_login(login, au, {"kind": "setup"}, ip, ua)

    # Existing user re-authenticating with Odoo (password reset flow). С записан
    # втори фактор setup флагът се вдига ЕДВА след кода — иначе Odoo паролата
    # сама би нулирала MCP паролата и би прескочила TOTP-а. Автозаписът на
    # 'default' също чака: до кода никой не пише в профила.
    odoo = {"url": url.rstrip("/"), "db": db, "uid": uid}
    if not _totp_enrolled(login):
        _autosave_default_connection(login, url, db, password, ip, ua)
    return _finish_login(login, au, {"kind": "setup", "odoo": odoo}, ip, ua)


async def _handle_setup_page(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)
    au = _load_user_auth(sess["login"])
    if not au or not au.get("setup_pending"):
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/dashboard", status_code=302)

    body = f"""
{_nav(sess)}
<div class="container py-5">
  <div class="row justify-content-center">
    <div class="col-lg-6">
      <div class="card shadow-sm">
        <div class="card-header brand-bg">
          <h4 class="mb-0"><i class="bi bi-key-fill"></i> Настрой MCP парола</h4>
        </div>
        <div class="card-body p-4">
          <p class="text-muted">Задайте парола, с която ще влизате в MCP админ конзолата отсега нататък. Минимум <strong>12 символа</strong>.</p>
          <form id="setupForm">
            <div class="mb-3">
              <label class="form-label">Нова парола</label>
              <input name="password" type="password" class="form-control" required minlength="12" autocomplete="new-password">
            </div>
            <div class="mb-3">
              <label class="form-label">Повторете паролата</label>
              <input name="password2" type="password" class="form-control" required minlength="12" autocomplete="new-password">
            </div>
            <button class="btn btn-brand w-100 py-2">Запази и продължи</button>
          </form>
          <div id="msg" class="mt-3"></div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
document.getElementById('setupForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  if (d.password !== d.password2) {{
    document.getElementById('msg').className = 'alert alert-danger small';
    document.getElementById('msg').textContent = 'Паролите не съвпадат';
    return;
  }}
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/setup-password', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{password: d.password}}), credentials:'include',
  }});
  const j = await r.json();
  if (r.ok) window.location.href = '{ADMIN_PATH_PREFIX}/dashboard';
  else {{
    document.getElementById('msg').className = 'alert alert-danger small';
    document.getElementById('msg').textContent = j.error || 'Грешка';
  }}
}});
</script>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell("MCP Admin · Настройка", body)))


async def _api_setup_password(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return JSONResponse({"error":"Unauthenticated"}, status_code=401)
    try:
        data = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error":"Bad request"}, status_code=400)
    pw = (data.get("password") or "").strip()
    if len(pw) < 12:
        return JSONResponse({"error":"Паролата трябва да е минимум 12 символа"}, status_code=400)
    au = _load_user_auth(sess["login"])
    if not au:
        return JSONResponse({"error":"User not found"}, status_code=404)
    au["password_hash"] = _hash_password(pw)
    au["setup_pending"] = False
    au["password_updated_at"] = _now()
    _save_user_auth(sess["login"], au)
    _audit(sess["login"], "password_set", "", _client_ip(req), req.headers.get("user-agent",""))
    return _apply_sec_headers(JSONResponse({"ok": True}))


async def _handle_dashboard(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)
    au = _load_user_auth(sess["login"])
    if not au:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)
    if au.get("setup_pending"):
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/setup", status_code=302)

    # Load user's connections
    conn_file = os.path.join(_user_dir(sess["login"]), "connections.json")
    conns = {}
    if os.path.isfile(conn_file):
        try:
            with open(conn_file) as f: conns = json.load(f)
        except (json.JSONDecodeError, OSError):
            conns = {}

    is_admin = bool(sess["is_admin"])
    users_count = len(_list_users()) if is_admin else 0
    tp = _totp_public(sess["login"])
    totp_badge = ('<span class="badge bg-success">включен</span>' if tp["enrolled"]
                  else '<span class="badge bg-secondary">изключен</span>')

    conns_html = ""
    if conns:
        def _sec_badges(cfg):
            out = []
            if isinstance(cfg.get("ssh"), dict) and any(cfg["ssh"].values()):
                out.append('<span class="badge bg-info text-dark" title="SSH"><i class="bi bi-terminal"></i></span>')
            if isinstance(cfg.get("portainer"), dict) and any(cfg["portainer"].values()):
                out.append('<span class="badge bg-success" title="Portainer"><i class="bi bi-boxes"></i></span>')
            if isinstance(cfg.get("web"), dict) and any(cfg["web"].values()):
                out.append('<span class="badge bg-warning text-dark" title="Web сесия"><i class="bi bi-globe"></i></span>')
            if isinstance(cfg.get("mcp"), dict) and any(cfg["mcp"].values()):
                out.append('<span class="badge bg-dark" title="MCP"><i class="bi bi-hdd-network"></i></span>')
            return " ".join(out)
        rows = []
        for alias, cfg in sorted(conns.items()):
            badges = _sec_badges(cfg)
            # 3.3.8: escape — стойностите идват от connections.json (import от GUI).
            a_e = html.escape(str(alias))
            url_e = html.escape(str(cfg.get('url', '') or ''))
            db_e = html.escape(str(cfg.get('db', '') or ''))
            rows.append(f"""
<tr>
  <td class="text-nowrap"><code>{a_e}</code>{(' ' + badges) if badges else ''}</td>
  <td class="small text-muted text-truncate" style="max-width: 260px;" title="{url_e}">{url_e}</td>
  <td class="small text-truncate" style="max-width: 140px;" title="{db_e}">{db_e}</td>
  <td class="text-end text-nowrap">
    <a href="{ADMIN_PATH_PREFIX}/connections#{a_e}" class="btn btn-sm btn-outline-primary" title="Редакция"><i class="bi bi-pencil"></i></a>
  </td>
</tr>""")
        conns_html = f"""
<div class="table-responsive">
  <table class="table table-sm align-middle mb-0">
    <thead class="small text-muted"><tr><th>Alias</th><th>URL</th><th>DB</th><th></th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>"""
    else:
        conns_html = """<p class="text-muted small">Нямате конфигурирани Odoo връзки.
        <a href="{}/connections" class="btn btn-sm btn-brand ms-2">Добави →</a></p>""".format(ADMIN_PATH_PREFIX)

    admin_card = ""
    if is_admin:
        admin_card = f"""
<div class="col-lg-4 mb-4">
  <div class="card shadow-sm h-100 border-warning">
    <div class="card-body">
      <h5 class="fw-bold"><i class="bi bi-shield-check text-warning"></i> Админ панел</h5>
      <p class="text-muted small">Управление на потребители, генериране на API keys за първа регистрация.</p>
      <p class="mb-2"><span class="badge bg-primary">{users_count}</span> регистрирани потребители</p>
      <a href="{ADMIN_PATH_PREFIX}/users" class="btn btn-warning">Open Users →</a>
    </div>
  </div>
</div>
"""

    body = f"""
{_nav(sess)}
<div class="container py-4">
  <div class="d-flex justify-content-between align-items-center mb-4">
    <div>
      <h2 class="mb-1">Добре дошъл, {sess['login'].split('@')[0]}</h2>
      <p class="text-muted mb-0">MCP Admin Dashboard</p>
    </div>
    <div>
      {'<span class="badge bg-warning text-dark fs-6">Admin · 7d session</span>' if is_admin else '<span class="badge bg-primary fs-6">User · 24h session</span>'}
    </div>
  </div>

  <div class="row">
    <div class="col-lg-8 mb-4">
      <div class="card shadow-sm h-100">
        <div class="card-body">
          <div class="d-flex justify-content-between align-items-center mb-3">
            <h5 class="fw-bold mb-0"><i class="bi bi-plug"></i> Моите Odoo връзки</h5>
            <a href="{ADMIN_PATH_PREFIX}/connections" class="btn btn-sm btn-brand">Управлявай</a>
          </div>
          {conns_html}
        </div>
      </div>
    </div>
    {admin_card}
  </div>

  <div class="row">
    <div class="col-lg-12">
      <div class="card shadow-sm">
        <div class="card-body">
          <h5 class="fw-bold"><i class="bi bi-gear"></i> Настройки на профила</h5>
          <div class="row">
            <div class="col-md-6">
              <p class="mb-2"><strong>Login:</strong> <code>{sess['login']}</code></p>
              <p class="mb-2"><strong>Роля:</strong> {'<span class="badge bg-warning text-dark">Admin</span>' if is_admin else '<span class="badge bg-primary">User</span>'}</p>
              <p class="mb-0"><strong>Session до:</strong> <span class="text-muted small">{datetime.fromtimestamp(sess['expires_at']).strftime('%Y-%m-%d %H:%M')}</span></p>
            </div>
            <div class="col-md-6">
              <p class="mb-2"><strong>Втори фактор:</strong> {totp_badge}</p>
              <a href="{ADMIN_PATH_PREFIX}/security" class="btn btn-outline-primary btn-sm">
                <i class="bi bi-shield-lock"></i> Сигурност: парола, 2FA, сесии
              </a>
              <a href="{ADMIN_PATH_PREFIX}/logout" class="btn btn-outline-secondary btn-sm">
                <i class="bi bi-box-arrow-right"></i> Изход
              </a>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell("MCP Admin · Dashboard", body)))


async def _handle_connections_page(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)
    au = _load_user_auth(sess["login"])
    if not au or au.get("setup_pending"):
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/setup", status_code=302)

    body = f"""
{_nav(sess)}
<div class="container py-4">
  <div class="d-flex justify-content-between align-items-center mb-2">
    <h2 class="mb-0"><i class="bi bi-plug-fill"></i> Odoo връзки</h2>
    <div>
      <button class="btn btn-outline-accent btn-sm me-1" data-bs-toggle="modal" data-bs-target="#importModal">
        <i class="bi bi-upload"></i> Import от GUI
      </button>
      <button class="btn btn-brand btn-sm" onclick="openEditor('','')">
        <i class="bi bi-plus-lg"></i> Нова връзка
      </button>
    </div>
  </div>
  <p class="text-muted small">Персоналните ти aliasi. Всеки може да има Odoo, SSH, Portainer, Web сесия и MCP линк — същите секции като в десктоп GUI-то.</p>

  <div class="card shadow-sm">
    <div class="card-body">
      <div id="connList">Зареждам…</div>
    </div>
  </div>
</div>

<!-- Editor Modal (Add + Edit) -->
<div class="modal fade" id="editorModal" tabindex="-1">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header brand-bg text-white">
        <h5 class="modal-title"><i class="bi bi-plug-fill"></i> <span id="editorTitle">Нова връзка</span></h5>
        <button class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <form id="connForm" autocomplete="off">
          <input type="hidden" name="_orig_alias" id="_orig_alias">
          <div class="mb-3">
            <label class="form-label small fw-bold">Alias <span class="text-danger">*</span></label>
            <input name="alias" id="fld_alias" class="form-control" required pattern="[a-z0-9_-]+" placeholder="myodoo, client-prod…">
            <div class="form-text">Малки букви, цифри, _ и -. Ползва се за <code>odoo_connect(alias=…)</code>.</div>
          </div>

          <ul class="nav nav-tabs" role="tablist">
            <li class="nav-item"><button type="button" class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-odoo"><i class="bi bi-database"></i> Odoo <span class="badge bg-danger ms-1">req</span></button></li>
            <li class="nav-item"><button type="button" class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-ssh"><i class="bi bi-terminal"></i> SSH <span id="badge-ssh" class="badge bg-secondary ms-1 d-none">set</span></button></li>
            <li class="nav-item"><button type="button" class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-port"><i class="bi bi-boxes"></i> Portainer <span id="badge-port" class="badge bg-secondary ms-1 d-none">set</span></button></li>
            <li class="nav-item"><button type="button" class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-web"><i class="bi bi-globe"></i> Web сесия <span id="badge-web" class="badge bg-secondary ms-1 d-none">set</span></button></li>
            <li class="nav-item"><button type="button" class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-mcp"><i class="bi bi-hdd-network"></i> MCP <span id="badge-mcp" class="badge bg-secondary ms-1 d-none">set</span></button></li>
          </ul>

          <div class="tab-content border border-top-0 rounded-bottom p-3 mb-3">
            <!-- Odoo -->
            <div class="tab-pane fade show active" id="tab-odoo">
              <div class="mb-2"><label class="form-label small">URL</label>
                <input name="url" type="url" class="form-control" placeholder="https://mycompany.odoo.com" required></div>
              <div class="row">
                <div class="col-md-6 mb-2"><label class="form-label small">Database</label>
                  <input name="db" class="form-control" required></div>
                <div class="col-md-6 mb-2"><label class="form-label small">Login</label>
                  <input name="user" type="email" class="form-control" required></div>
              </div>
              <div class="mb-2"><label class="form-label small">API key</label>
                <input name="api_key" type="password" class="form-control" placeholder="••• (оставi празно за да запазиш съществуващия)">
                <div class="form-text">Odoo → Preferences → Account Security → New API Key</div>
              </div>
              <div class="form-check"><input class="form-check-input" type="checkbox" name="verify_ssl" value="1" id="fld_vs" checked>
                <label class="form-check-label small" for="fld_vs">Verify SSL certificate</label></div>
            </div>

            <!-- SSH -->
            <div class="tab-pane fade" id="tab-ssh">
              <p class="text-muted small mb-2">За <code>ssh_execute</code> и git операции. Празно = секцията не се пази.</p>
              <div class="row">
                <div class="col-md-8 mb-2"><label class="form-label small">Host</label>
                  <input name="ssh.host" class="form-control" placeholder="1.2.3.4 или example.com"></div>
                <div class="col-md-4 mb-2"><label class="form-label small">Port</label>
                  <input name="ssh.port" type="number" class="form-control" value="22"></div>
              </div>
              <div class="row">
                <div class="col-md-6 mb-2"><label class="form-label small">User</label>
                  <input name="ssh.user" class="form-control" placeholder="root"></div>
                <div class="col-md-6 mb-2"><label class="form-label small">Auth</label>
                  <select name="ssh.auth" class="form-select">
                    <option value="agent" selected>SSH agent</option>
                    <option value="key">Identity file</option>
                    <option value="password">Password</option>
                  </select></div>
              </div>
              <div class="mb-2"><label class="form-label small">Identity file (когато Auth = key)</label>
                <input name="ssh.identity_file" type="password" class="form-control" placeholder="/home/user/.ssh/id_ed25519"></div>
              <div class="mb-2"><label class="form-label small">Password (когато Auth = password)</label>
                <input name="ssh.password" type="password" class="form-control"></div>
            </div>

            <!-- Portainer -->
            <div class="tab-pane fade" id="tab-port">
              <p class="text-muted small mb-2">За Portainer MCP инструменти (<code>portainer__*</code>).</p>
              <div class="mb-2"><label class="form-label small">Portainer URL</label>
                <input name="portainer.url" type="url" class="form-control" placeholder="https://portainer.example.com"></div>
              <div class="mb-2"><label class="form-label small">API token</label>
                <input name="portainer.token" type="password" class="form-control" placeholder="ptr_..."></div>
              <div class="row">
                <div class="col-md-6 form-check ms-1">
                  <input class="form-check-input" type="checkbox" name="portainer.ssl_verify" value="1" id="fld_ps" checked>
                  <label class="form-check-label small" for="fld_ps">Verify SSL</label></div>
                <div class="col-md-6 form-check ms-1">
                  <input class="form-check-input" type="checkbox" name="portainer.read_only" value="1" id="fld_pr">
                  <label class="form-check-label small" for="fld_pr">Read-only</label></div>
              </div>
            </div>

            <!-- Web -->
            <div class="tab-pane fade" id="tab-web">
              <p class="text-muted small mb-2">За <code>odoo_web_*</code> (XLSX/export, session API). Обикновено same URL и login както Odoo, но с password вместо API key.</p>
              <div class="mb-2"><label class="form-label small">Web URL (default: същото като Odoo URL)</label>
                <input name="web.url" type="url" class="form-control" placeholder="https://mycompany.odoo.com"></div>
              <div class="mb-2"><label class="form-label small">Database (default: същата)</label>
                <input name="web.db" class="form-control"></div>
              <div class="mb-2"><label class="form-label small">Login</label>
                <input name="web.login" type="email" class="form-control"></div>
              <div class="mb-2"><label class="form-label small">Password</label>
                <input name="web.password" type="password" class="form-control"></div>
            </div>

            <!-- MCP -->
            <div class="tab-pane fade" id="tab-mcp">
              <p class="text-muted small mb-2">Ако тази връзка е и MCP gateway (mcp.odoo-shell.space, etc.).</p>
              <div class="mb-2"><label class="form-label small">MCP URL</label>
                <input name="mcp.url" type="url" class="form-control" placeholder="https://mcp.example.com"></div>
              <div class="mb-2"><label class="form-label small">MCP token</label>
                <input name="mcp.token" type="password" class="form-control"></div>
            </div>
          </div>

          <div id="connMsg" class="mb-2"></div>
          <div class="d-flex justify-content-end gap-2">
            <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Откажи</button>
            <button type="submit" class="btn btn-brand"><i class="bi bi-save"></i> Запази</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</div>

<!-- Import Modal -->
<div class="modal fade" id="importModal" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header brand-bg text-white">
        <h5 class="modal-title"><i class="bi bi-upload"></i> Импорт от локален GUI</h5>
        <button class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <p class="small text-muted">
          Отвори <code>~/.claude/odoo_connections/connections.json</code>, copy цялото съдържание и paste долу.
          Поддържа dict <code>{{alias: cfg}}</code> или list <code>[{{alias, url, db, api_key, ssh, portainer, web, ...}}]</code>.
          Nested секции (ssh/portainer/web/mcp) се запазват.
        </p>
        <textarea id="importJson" class="form-control font-monospace" rows="14" placeholder='{{ "teolino": {{"url":"https://erp...", "db":"...", "api_key":"...", "ssh":{{"host":"..."}}}} }}'></textarea>
        <div class="form-check mt-3">
          <input class="form-check-input" type="checkbox" id="replaceExisting">
          <label class="form-check-label small" for="replaceExisting">
            Overwrite съществуващи aliasi със същото име (default: skip)
          </label>
        </div>
        <div id="importMsg" class="mt-3"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-bs-dismiss="modal">Затвори</button>
        <button class="btn btn-brand" id="doImport"><i class="bi bi-upload"></i> Импортирай</button>
      </div>
    </div>
  </div>
</div>

<script>
const PATH = '{ADMIN_PATH_PREFIX}';
let _csrf = null;
async function csrf() {{
  if (_csrf) return _csrf;
  const j = await fetch(PATH + '/api/csrf', {{credentials:'include'}}).then(r => r.json());
  _csrf = j.token; return _csrf;
}}

function hasSection(cfg, name) {{
  const s = cfg[name];
  if (!s || typeof s !== 'object') return false;
  return Object.values(s).some(v => v !== '' && v !== false && v != null);
}}

function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function sectionBadges(cfg) {{
  const badges = [];
  if (hasSection(cfg, 'ssh')) badges.push('<span class="badge bg-info text-dark"><i class="bi bi-terminal"></i> SSH</span>');
  if (hasSection(cfg, 'portainer')) badges.push('<span class="badge bg-success"><i class="bi bi-boxes"></i> Portainer</span>');
  if (hasSection(cfg, 'web')) badges.push('<span class="badge bg-warning text-dark"><i class="bi bi-globe"></i> Web</span>');
  if (hasSection(cfg, 'mcp')) badges.push('<span class="badge bg-dark"><i class="bi bi-hdd-network"></i> MCP</span>');
  return badges.join(' ');
}}

async function loadConns() {{
  const r = await fetch(PATH + '/api/connections', {{credentials:'include'}});
  const j = await r.json();
  const list = document.getElementById('connList');
  if (!j.connections || !Object.keys(j.connections).length) {{
    list.innerHTML = '<p class="text-muted small mb-0">Няма регистрирани връзки. Натисни <strong>Нова връзка</strong> или <strong>Import от GUI</strong>.</p>';
    return;
  }}
  let rows = '';
  for (const [alias, cfg] of Object.entries(j.connections)) {{
    const badges = sectionBadges(cfg);
    const a = esc(alias);
    rows += `
      <tr>
        <td><code class="fs-6">${{a}}</code><div class="mt-1">${{badges}}</div></td>
        <td class="small text-muted"><div>${{esc(cfg.url||'')}}</div><div>${{esc(cfg.db||'')}} · ${{esc(cfg.user||'')}}</div></td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-primary" onclick="openEditor('${{a}}','edit')"><i class="bi bi-pencil"></i> Редакция</button>
          <button class="btn btn-sm btn-outline-danger" onclick="delConn('${{a}}')"><i class="bi bi-trash"></i></button>
        </td>
      </tr>`;
  }}
  list.innerHTML = `<div class="table-responsive"><table class="table align-middle"><thead class="small text-muted"><tr><th>Alias / секции</th><th>Odoo</th><th></th></tr></thead><tbody>${{rows}}</tbody></table></div>`;
}}

async function delConn(alias) {{
  if (!confirm('Изтрий ' + alias + '?')) return;
  const t = await csrf();
  const r = await fetch(PATH + '/api/connections/' + encodeURIComponent(alias), {{
    method:'DELETE', credentials:'include', headers: {{'X-CSRF-Token': t}},
  }});
  if (r.ok) loadConns(); else alert('Error: ' + r.status);
}}

function clearForm() {{
  const f = document.getElementById('connForm');
  f.reset();
  document.getElementById('_orig_alias').value = '';
  document.getElementById('fld_vs').checked = true;
  document.getElementById('fld_ps').checked = true;
  document.getElementById('fld_pr').checked = false;
  for (const b of ['ssh','port','web','mcp']) {{
    document.getElementById('badge-' + b).classList.add('d-none');
  }}
  document.getElementById('connMsg').innerHTML = '';
}}

function fillForm(alias, cfg) {{
  clearForm();
  document.getElementById('_orig_alias').value = alias || '';
  document.getElementById('fld_alias').value = alias || '';
  document.getElementById('fld_alias').readOnly = !!alias;  // lock alias on edit
  const set = (name, val) => {{ const el = document.querySelector(`[name="${{name}}"]`); if (el) el.value = val ?? ''; }};
  const setCheck = (name, val) => {{ const el = document.querySelector(`[name="${{name}}"]`); if (el) el.checked = !!val; }};
  set('url', cfg.url); set('db', cfg.db); set('user', cfg.user); set('api_key', cfg.api_key);
  setCheck('verify_ssl', cfg.verify_ssl !== false);
  const ssh = cfg.ssh || {{}};
  set('ssh.host', ssh.host); set('ssh.port', ssh.port || 22); set('ssh.user', ssh.user);
  set('ssh.auth', ssh.auth || 'agent'); set('ssh.identity_file', ssh.identity_file); set('ssh.password', ssh.password);
  if (hasSection(cfg,'ssh')) document.getElementById('badge-ssh').classList.remove('d-none');
  const p = cfg.portainer || {{}};
  set('portainer.url', p.url); set('portainer.token', p.token);
  setCheck('portainer.ssl_verify', p.ssl_verify !== false);
  setCheck('portainer.read_only', !!p.read_only);
  if (hasSection(cfg,'portainer')) document.getElementById('badge-port').classList.remove('d-none');
  const w = cfg.web || {{}};
  set('web.url', w.url); set('web.db', w.db); set('web.login', w.login); set('web.password', w.password);
  if (hasSection(cfg,'web')) document.getElementById('badge-web').classList.remove('d-none');
  const m = cfg.mcp || {{}};
  set('mcp.url', m.url); set('mcp.token', m.token);
  if (hasSection(cfg,'mcp')) document.getElementById('badge-mcp').classList.remove('d-none');
}}

async function openEditor(alias, mode) {{
  const modal = new bootstrap.Modal(document.getElementById('editorModal'));
  if (alias && mode === 'edit') {{
    const r = await fetch(PATH + '/api/connections/' + encodeURIComponent(alias), {{credentials:'include'}});
    if (!r.ok) {{ alert('Load failed'); return; }}
    const j = await r.json();
    document.getElementById('editorTitle').textContent = 'Редакция · ' + alias;
    fillForm(alias, j.config || {{}});
  }} else {{
    clearForm();
    document.getElementById('fld_alias').readOnly = false;
    document.getElementById('editorTitle').textContent = 'Нова връзка';
  }}
  modal.show();
  // reset to first tab
  const firstTab = new bootstrap.Tab(document.querySelector('#editorModal .nav-link'));
  firstTab.show();
}}

function collectForm() {{
  const f = document.getElementById('connForm');
  const fd = new FormData(f);
  const payload = {{}};
  for (const [k, v] of fd.entries()) {{
    if (k.startsWith('_')) continue;
    if (k.includes('.')) {{
      const [sec, sub] = k.split('.');
      payload[sec] = payload[sec] || {{}};
      payload[sec][sub] = v;
    }} else {{
      payload[k] = v;
    }}
  }}
  payload.verify_ssl = !!f.querySelector('[name="verify_ssl"]:checked');
  if (payload.portainer) {{
    payload.portainer.ssl_verify = !!f.querySelector('[name="portainer.ssl_verify"]:checked');
    payload.portainer.read_only = !!f.querySelector('[name="portainer.read_only"]:checked');
    if (payload.portainer.port) payload.portainer.port = parseInt(payload.portainer.port, 10) || undefined;
  }}
  if (payload.ssh && payload.ssh.port) {{
    payload.ssh.port = parseInt(payload.ssh.port, 10) || 22;
  }}
  // Drop sections that are completely empty (all values falsy)
  for (const sec of ['ssh','portainer','web','mcp']) {{
    if (!payload[sec]) continue;
    const anyVal = Object.entries(payload[sec]).some(([k,v]) => {{
      if (typeof v === 'boolean') return false;  // booleans alone don't count
      return v !== '' && v != null;
    }});
    if (!anyVal) delete payload[sec];
  }}
  return payload;
}}

document.getElementById('connForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const msg = document.getElementById('connMsg');
  const origAlias = document.getElementById('_orig_alias').value;
  const payload = collectForm();
  const t = await csrf();
  let r;
  if (origAlias) {{
    // PUT update — don't re-send alias (it's fixed)
    delete payload.alias;
    r = await fetch(PATH + '/api/connections/' + encodeURIComponent(origAlias), {{
      method:'PUT', credentials:'include',
      headers:{{'Content-Type':'application/json','X-CSRF-Token': t}},
      body: JSON.stringify(payload),
    }});
  }} else {{
    r = await fetch(PATH + '/api/connections', {{
      method:'POST', credentials:'include',
      headers:{{'Content-Type':'application/json','X-CSRF-Token': t}},
      body: JSON.stringify(payload),
    }});
  }}
  const j = await r.json().catch(() => ({{}}));
  if (r.ok) {{
    msg.className='alert alert-success small'; msg.textContent='Запазено';
    setTimeout(() => {{
      bootstrap.Modal.getInstance(document.getElementById('editorModal')).hide();
      loadConns();
    }}, 400);
  }} else {{
    msg.className='alert alert-danger small'; msg.textContent = j.error || ('HTTP ' + r.status);
  }}
}});

document.getElementById('doImport').addEventListener('click', async () => {{
  const payload = document.getElementById('importJson').value.trim();
  const replace = document.getElementById('replaceExisting').checked;
  const m = document.getElementById('importMsg');
  if (!payload) {{ m.className='alert alert-warning small'; m.textContent='Paste JSON-a първо'; return; }}
  const t = await csrf();
  const r = await fetch(PATH + '/api/connections/import', {{
    method:'POST', credentials:'include',
    headers:{{'Content-Type':'application/json','X-CSRF-Token': t}},
    body: JSON.stringify({{payload, replace}}),
  }});
  const j = await r.json();
  if (r.ok) {{
    m.className='alert alert-success small';
    m.innerHTML = `✓ Added: <strong>${{j.added}}</strong>, Updated: <strong>${{j.updated}}</strong>, Skipped: <strong>${{j.skipped}}</strong> · Total: ${{j.total}}`;
    loadConns();
  }} else {{
    m.className='alert alert-danger small'; m.textContent = j.error || 'Error';
  }}
}});

// Open editor via ?edit=alias (from dashboard Редактирай link with fragment)
(function() {{
  const hash = (location.hash || '').replace(/^#/, '');
  if (hash) {{ openEditor(hash, 'edit'); history.replaceState(null, '', location.pathname); }}
}})();

loadConns();
</script>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell("MCP Admin · Connections", body)))


async def _api_csrf(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return JSONResponse({"error":"Unauthenticated"}, status_code=401)
    return _apply_sec_headers(JSONResponse({"token": sess["csrf_token"]}))


def _check_csrf(req: Request, sess: dict) -> bool:
    token = req.headers.get("x-csrf-token") or ""
    return bool(token) and hmac.compare_digest(token, sess.get("csrf_token", ""))


def _load_connections(login: str) -> dict:
    conn_file = os.path.join(_user_dir(login), "connections.json")
    if not os.path.isfile(conn_file):
        return {}
    try:
        with open(conn_file) as f: return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_connections(login: str, data: dict) -> None:
    conn_file = os.path.join(_user_dir(login), "connections.json")
    os.makedirs(_user_dir(login), exist_ok=True)
    with open(conn_file, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(conn_file, 0o600)


_MASK = "•••"
_SECRET_PATHS = [
    ("api_key",),
    ("ssh", "password"),
    ("ssh", "identity_file"),
    ("portainer", "token"),
    ("web", "password"),
    ("mcp", "token"),
]


def _mask_config(cfg: dict) -> dict:
    """Return a deep copy with secret fields replaced by _MASK (but only if non-empty).
    Empty strings stay empty so the UI can tell a blank field from a masked one."""
    out = json.loads(json.dumps(cfg))  # deep copy
    for path in _SECRET_PATHS:
        node = out
        for seg in path[:-1]:
            if not isinstance(node, dict) or seg not in node:
                node = None
                break
            node = node[seg]
        if isinstance(node, dict):
            key = path[-1]
            if node.get(key):
                node[key] = _MASK
    return out


def _unmask_merge(existing: dict, incoming: dict) -> dict:
    """Merge `incoming` into a deep copy of `existing`, treating _MASK values as 'keep existing'.
    Any explicit None or '' in incoming clears the field.
    Unknown nested sections are dropped (whitelist only)."""
    allowed_root = {"url","db","user","api_key","verify_ssl","protocol","http_proxy","ssh","portainer","web","mcp"}
    allowed_ssh = {"host","port","user","auth","identity_file","password"}
    allowed_portainer = {"url","token","ssl_verify","read_only"}
    allowed_web = {"url","db","login","password"}
    allowed_mcp = {"url","token"}

    merged = json.loads(json.dumps(existing)) if existing else {}

    def _set(dst, key, val):
        if val == _MASK:
            return  # keep existing
        dst[key] = val

    for k, v in incoming.items():
        if k not in allowed_root:
            continue
        if k == "ssh" and isinstance(v, dict):
            cur = merged.get("ssh") or {}
            if not isinstance(cur, dict): cur = {}
            for sk, sv in v.items():
                if sk in allowed_ssh: _set(cur, sk, sv)
            # drop section if completely empty
            if any(str(cur.get(x, "")).strip() for x in allowed_ssh):
                merged["ssh"] = cur
            elif "ssh" in merged:
                merged.pop("ssh", None)
        elif k == "portainer" and isinstance(v, dict):
            cur = merged.get("portainer") or {}
            if not isinstance(cur, dict): cur = {}
            for sk, sv in v.items():
                if sk in allowed_portainer: _set(cur, sk, sv)
            if any(str(cur.get(x, "")).strip() for x in allowed_portainer):
                merged["portainer"] = cur
            elif "portainer" in merged:
                merged.pop("portainer", None)
        elif k == "web" and isinstance(v, dict):
            cur = merged.get("web") or {}
            if not isinstance(cur, dict): cur = {}
            for sk, sv in v.items():
                if sk in allowed_web: _set(cur, sk, sv)
            if any(str(cur.get(x, "")).strip() for x in allowed_web):
                merged["web"] = cur
            elif "web" in merged:
                merged.pop("web", None)
        elif k == "mcp" and isinstance(v, dict):
            cur = merged.get("mcp") or {}
            if not isinstance(cur, dict): cur = {}
            for sk, sv in v.items():
                if sk in allowed_mcp: _set(cur, sk, sv)
            if any(str(cur.get(x, "")).strip() for x in allowed_mcp):
                merged["mcp"] = cur
            elif "mcp" in merged:
                merged.pop("mcp", None)
        elif k == "url":
            _set(merged, "url", (v or "").rstrip("/"))
        elif k == "verify_ssl":
            merged["verify_ssl"] = bool(v)
        else:
            _set(merged, k, v)
    return merged


async def _api_connections(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return JSONResponse({"error":"Unauthenticated"}, status_code=401)
    login = sess["login"]
    if req.method == "GET":
        data = _load_connections(login)
        safe = {k: _mask_config(v) for k, v in data.items()}
        return _apply_sec_headers(JSONResponse({"connections": safe}))
    if req.method == "POST":
        if not _check_csrf(req, sess):
            return JSONResponse({"error":"CSRF failure"}, status_code=403)
        try:
            body = await req.json()
        except json.JSONDecodeError:
            return JSONResponse({"error":"Bad request"}, status_code=400)
        alias = (body.get("alias") or "").strip().lower()
        if not alias or not alias.replace("_","").replace("-","").isalnum():
            return JSONResponse({"error":"Невалиден alias (само a-z 0-9 _ -)"}, status_code=400)
        data = _load_connections(login)
        incoming = {k: v for k, v in body.items() if k != "alias"}
        if "protocol" not in incoming:
            incoming["protocol"] = "xmlrpc"
        data[alias] = _unmask_merge(data.get(alias, {}), incoming)
        _save_connections(login, data)
        _audit(login, "connection_add", alias, _client_ip(req), req.headers.get("user-agent",""))
        return _apply_sec_headers(JSONResponse({"ok": True}))


async def _api_connections_import(req: Request):
    """Bulk import connections from pasted JSON (local GUI export).
    Accepts either dict {alias: cfg} or list of records."""
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return JSONResponse({"error":"Unauthenticated"}, status_code=401)
    if not _check_csrf(req, sess):
        return JSONResponse({"error":"CSRF failure"}, status_code=403)
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return JSONResponse({"error":"Bad JSON"}, status_code=400)
    raw = body.get("payload")
    if not raw:
        return JSONResponse({"error":"Empty payload"}, status_code=400)

    # Accept either a string (JSON paste) or already-parsed object
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            return JSONResponse({"error": f"Parse: {e}"}, status_code=400)

    # Normalize into {alias: cfg}
    incoming = {}
    if isinstance(raw, dict):
        for alias, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            incoming[alias] = cfg
    elif isinstance(raw, list):
        # GUI sometimes exports as list [{"alias":.., "url":...}, ...]
        for r in raw:
            if not isinstance(r, dict): continue
            alias = r.get("alias") or r.get("name")
            if not alias: continue
            incoming[alias] = {k:v for k,v in r.items() if k not in ("alias","name")}
    if not incoming:
        return JSONResponse({"error":"No valid connections in payload"}, status_code=400)

    conn_file = os.path.join(_user_dir(sess["login"]), "connections.json")
    data = {}
    if os.path.isfile(conn_file):
        try:
            with open(conn_file) as f: data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    replace = bool(body.get("replace", False))
    added, updated, skipped = 0, 0, 0
    for alias, cfg in incoming.items():
        clean_alias = alias.strip().lower()
        if not clean_alias.replace("_","").replace("-","").isalnum():
            skipped += 1
            continue
        # Accept the GUI's richer format verbatim (ssh / portainer / web / mcp nested sections),
        # normalizing a few synonym keys on the top level.
        entry = {
            "url": (cfg.get("url") or "").rstrip("/"),
            "db": cfg.get("db") or cfg.get("database") or "",
            "user": cfg.get("user") or cfg.get("username") or cfg.get("login") or "",
            "api_key": cfg.get("api_key") or cfg.get("apikey") or "",
            "protocol": cfg.get("protocol") or "xmlrpc",
            "verify_ssl": bool(cfg.get("verify_ssl", True)),
        }
        for section in ("ssh", "portainer", "web", "mcp"):
            if isinstance(cfg.get(section), dict):
                entry[section] = cfg[section]
        if cfg.get("http_proxy"):
            entry["http_proxy"] = cfg["http_proxy"]
        if clean_alias in data and not replace:
            skipped += 1
            continue
        if clean_alias in data:
            updated += 1
        else:
            added += 1
        data[clean_alias] = entry

    os.makedirs(_user_dir(sess["login"]), exist_ok=True)
    with open(conn_file, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(conn_file, 0o600)
    _audit(sess["login"], "connections_import", "", _client_ip(req), req.headers.get("user-agent",""),
           {"added": added, "updated": updated, "skipped": skipped})
    return _apply_sec_headers(JSONResponse({
        "ok": True, "added": added, "updated": updated, "skipped": skipped,
        "total": len(data),
    }))


async def _api_connection_crud(req: Request):
    """GET/PUT/DELETE a single connection alias."""
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return JSONResponse({"error":"Unauthenticated"}, status_code=401)
    alias = (req.path_params.get("alias", "") or "").strip().lower()
    if not alias:
        return JSONResponse({"error":"Missing alias"}, status_code=400)
    login = sess["login"]
    data = _load_connections(login)

    if req.method == "GET":
        if alias not in data:
            return JSONResponse({"error":"Not found"}, status_code=404)
        return _apply_sec_headers(JSONResponse({"alias": alias, "config": _mask_config(data[alias])}))

    if req.method == "PUT":
        if not _check_csrf(req, sess):
            return JSONResponse({"error":"CSRF failure"}, status_code=403)
        try:
            body = await req.json()
        except json.JSONDecodeError:
            return JSONResponse({"error":"Bad request"}, status_code=400)
        if alias not in data:
            return JSONResponse({"error":"Not found"}, status_code=404)
        data[alias] = _unmask_merge(data[alias], body)
        _save_connections(login, data)
        _audit(login, "connection_update", alias, _client_ip(req), req.headers.get("user-agent",""))
        return _apply_sec_headers(JSONResponse({"ok": True}))

    if req.method == "DELETE":
        if not _check_csrf(req, sess):
            return JSONResponse({"error":"CSRF failure"}, status_code=403)
        if alias not in data:
            return JSONResponse({"error":"Not found"}, status_code=404)
        data.pop(alias)
        _save_connections(login, data)
        _audit(login, "connection_delete", alias, _client_ip(req), req.headers.get("user-agent",""))
        return _apply_sec_headers(JSONResponse({"ok": True}))

    return JSONResponse({"error":"Method not allowed"}, status_code=405)


async def _handle_users_page(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess or not sess["is_admin"]:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/dashboard", status_code=302)

    body = f"""
{_nav(sess)}
<div class="container py-4">
  <h2 class="mb-1"><i class="bi bi-people-fill text-warning"></i> Потребители</h2>
  <p class="text-muted">Създавайте нови потребители с еднократен API key за първа регистрация.</p>

  <div class="row">
    <div class="col-lg-5 mb-4">
      <div class="card shadow-sm">
        <div class="card-header brand-bg"><h5 class="mb-0">Създай нов потребител</h5></div>
        <div class="card-body">
          <form id="newUserForm">
            <div class="mb-3">
              <label class="form-label small fw-semibold">Odoo login (email)</label>
              <input name="login" type="email" class="form-control" required>
              <small class="text-muted">Същият login, който потребителят използва в Odoo</small>
            </div>
            <div class="form-check mb-3">
              <input class="form-check-input" type="checkbox" name="admin" id="adm">
              <label class="form-check-label small" for="adm">Дай admin права</label>
            </div>
            <button class="btn btn-warning w-100">Създай + генерирай API key</button>
          </form>
          <div id="newUserMsg" class="mt-3"></div>
        </div>
      </div>
    </div>
    <div class="col-lg-7 mb-4">
      <div class="card shadow-sm">
        <div class="card-body">
          <h5 class="fw-bold">Всички потребители</h5>
          <div id="userList">Зареждам...</div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
async function loadUsers() {{
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/users', {{credentials:'include'}});
  const j = await r.json();
  const list = document.getElementById('userList');
  if (!j.users || !j.users.length) {{ list.innerHTML='<p class="text-muted small">Няма потребители.</p>'; return; }}
  let rows = '';
  for (const u of j.users) {{
    const bg = u.admin ? 'bg-warning text-dark' : 'bg-primary';
    const badge = u.admin ? 'admin' : 'user';
    const state = u.setup_pending ? '<span class="badge bg-secondary">чака setup</span>' : '<span class="badge bg-success">активен</span>';
    const totp = u.totp ? '<span class="badge bg-success" title="TOTP включен">2FA</span>' : '<span class="badge bg-light text-muted border">—</span>';
    const reset = u.totp ? `<button class="btn btn-sm btn-outline-danger ms-1" onclick="resetTotp('${{u.login}}')" title="Нулирай втория фактор"><i class="bi bi-shield-x"></i> 2FA</button>` : '';
    rows += `
      <tr>
        <td><code>${{u.login}}</code></td>
        <td><span class="badge ${{bg}}">${{badge}}</span></td>
        <td>${{state}}</td>
        <td>${{totp}}</td>
        <td class="small text-muted">${{u.created}}</td>
        <td class="text-end text-nowrap">
          <button class="btn btn-sm btn-outline-warning" onclick="regenKey('${{u.login}}')"><i class="bi bi-key"></i> Нов key</button>${{reset}}
        </td>
      </tr>`;
  }}
  list.innerHTML = `<div class="table-responsive"><table class="table table-sm table-mono"><thead><tr><th>Login</th><th>Role</th><th>Status</th><th>2FA</th><th>Created</th><th></th></tr></thead><tbody>${{rows}}</tbody></table></div>`;
}}

async function resetTotp(login) {{
  if (!confirm('Нулирай втория фактор на ' + login + '? Следващият вход ще е само с парола, докато не го включи отново.')) return;
  const csrf = await fetch('{ADMIN_PATH_PREFIX}/api/csrf', {{credentials:'include'}}).then(r => r.json());
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/users/' + encodeURIComponent(login) + '/totp-reset', {{
    method:'POST', credentials:'include', headers: {{'X-CSRF-Token': csrf.token}},
  }});
  const j = await r.json();
  if (r.ok) loadUsers(); else alert(j.error || 'Грешка');
}}

async function regenKey(login) {{
  if (!confirm('Генерирай нов API key за ' + login + '? Старата парола се анулира; потребителят въвежда key-а при следващо влизане през Odoo таба.')) return;
  const csrf = await fetch('{ADMIN_PATH_PREFIX}/api/csrf', {{credentials:'include'}}).then(r => r.json());
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/users/' + encodeURIComponent(login) + '/genkey', {{
    method:'POST', credentials:'include',
    headers: {{'X-CSRF-Token': csrf.token}},
  }});
  const j = await r.json();
  if (r.ok) {{
    document.getElementById('newUserMsg').innerHTML = `
      <div class="alert alert-warning">
        <strong>API key (показва се веднъж):</strong>
        <code class="apikey">${{j.api_key}}</code>
        <small>Предайте го на потребителя по сигурен канал. Key-ят валиден 7 дни.</small>
      </div>`;
    loadUsers();
  }} else alert(j.error || 'Грешка');
}}

document.getElementById('newUserForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  d.admin = !!d.admin;
  const csrf = await fetch('{ADMIN_PATH_PREFIX}/api/csrf', {{credentials:'include'}}).then(r => r.json());
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/users', {{
    method:'POST', credentials:'include',
    headers:{{'Content-Type':'application/json','X-CSRF-Token': csrf.token}},
    body: JSON.stringify(d),
  }});
  const j = await r.json();
  const m = document.getElementById('newUserMsg');
  if (r.ok) {{
    m.innerHTML = `
      <div class="alert alert-warning">
        <strong>Потребител ${{d.login}} създаден. API key (показва се веднъж):</strong>
        <code class="apikey">${{j.api_key}}</code>
        <small>Предайте го по сигурен канал. Валиден 7 дни — потребителят го въвежда в "Odoo" таба на /login като парола.</small>
      </div>`;
    e.target.reset(); loadUsers();
  }} else {{ m.className='alert alert-danger'; m.textContent = j.error || 'Грешка'; }}
}});

loadUsers();
</script>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell("MCP Admin · Users", body)))


async def _api_users(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess or not sess["is_admin"]:
        return JSONResponse({"error":"Admin only"}, status_code=403)

    if req.method == "GET":
        out = []
        for login in _list_users():
            au = _load_user_auth(login) or {}
            out.append({
                "login": login,
                "admin": bool(au.get("admin")),
                "setup_pending": bool(au.get("setup_pending")),
                "totp": _totp_enrolled(login),
                "created": datetime.fromtimestamp(au.get("created_at", 0)).strftime("%Y-%m-%d") if au.get("created_at") else "",
            })
        return _apply_sec_headers(JSONResponse({"users": out}))

    if req.method == "POST":
        if not _check_csrf(req, sess):
            return JSONResponse({"error":"CSRF failure"}, status_code=403)
        try:
            data = await req.json()
        except json.JSONDecodeError:
            return JSONResponse({"error":"Bad request"}, status_code=400)
        login = _sanitize_login(data.get("login") or "")
        is_adm = bool(data.get("admin"))
        if not login or "@" not in (data.get("login") or ""):
            return JSONResponse({"error":"Невалиден email"}, status_code=400)
        if _load_user_auth(login):
            return JSONResponse({"error":"Потребителят вече съществува"}, status_code=409)
        api_key = _gen_api_key()
        au = {
            "login": login,
            "admin": is_adm,
            "created_at": _now(),
            "created_by": sess["login"],
            "setup_pending": True,
            "password_hash": "",
            "api_key_hash": _hash_api_key(api_key),
            "api_key_expires": _now() + 7 * 86400,
        }
        _save_user_auth(login, au)
        _audit(sess["login"], "user_create", login, _client_ip(req), req.headers.get("user-agent",""), {"admin": is_adm})
        return _apply_sec_headers(JSONResponse({"ok": True, "api_key": api_key}))


async def _api_user_genkey(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess or not sess["is_admin"]:
        return JSONResponse({"error":"Admin only"}, status_code=403)
    if not _check_csrf(req, sess):
        return JSONResponse({"error":"CSRF failure"}, status_code=403)
    target = _sanitize_login(req.path_params.get("login", ""))
    au = _load_user_auth(target)
    if not au:
        return JSONResponse({"error":"Not found"}, status_code=404)
    api_key = _gen_api_key()
    au["api_key_hash"] = _hash_api_key(api_key)
    au["api_key_expires"] = _now() + 7 * 86400
    au["setup_pending"] = True
    au["password_hash"] = ""   # invalidate old password
    _save_user_auth(target, au)
    _audit(sess["login"], "user_genkey", target, _client_ip(req), req.headers.get("user-agent",""))
    return _apply_sec_headers(JSONResponse({"ok": True, "api_key": api_key}))


# ─── Общи парчета за JSON API-тата (3.3.8) ───────────────────
def _api_session(req: Request, csrf: bool = False, admin: bool = False) -> tuple[dict | None, Response | None]:
    """(сесия, None) или (None, отговор за връщане)."""
    gate = _gate(req)
    if gate:
        return None, gate
    sess = _read_session(req)
    if not sess:
        return None, JSONResponse({"error": "Unauthenticated"}, status_code=401)
    if admin and not sess["is_admin"]:
        return None, JSONResponse({"error": "Admin only"}, status_code=403)
    if csrf and not _check_csrf(req, sess):
        return None, JSONResponse({"error": "CSRF failure"}, status_code=403)
    return sess, None


async def _json_body(req: Request) -> dict:
    try:
        data = await req.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _req_meta(req: Request) -> tuple[str, str]:
    return _client_ip(req), req.headers.get("user-agent", "")


# ─── Втори фактор при вход (3.3.8) ───────────────────────────
async def _handle_totp_page(req: Request):
    gate = _gate(req)
    if gate: return gate
    if _read_session(req):
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/dashboard", status_code=302)
    pre = _read_preauth(req)
    if not pre:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)
    login_e = html.escape(pre["login"])
    body = f"""
<div class="gradient-bg d-flex align-items-center py-5">
  <div class="container">
    <div class="text-center text-white mb-4">
      <i class="bi bi-shield-lock-fill" style="font-size:3rem;"></i>
      <h1 class="mt-2 mb-1">Втори фактор</h1>
      <p class="opacity-75">{login_e} · кодът от authenticator приложението</p>
    </div>
    <div class="login-card card shadow-lg border-0">
      <div class="card-body p-4 p-md-5">
        <form id="totpForm">
          <div class="mb-3" id="codeBox">
            <label class="form-label small fw-semibold">6-цифрен код</label>
            <input name="code" type="text" class="form-control form-control-lg text-center" inputmode="numeric"
                   pattern="[0-9 ]*" maxlength="7" autocomplete="one-time-code" autofocus>
          </div>
          <div class="mb-3 d-none" id="recBox">
            <label class="form-label small fw-semibold">Код за възстановяване</label>
            <input name="recovery_code" type="text" class="form-control form-control-lg text-center"
                   placeholder="xxxxx-xxxxx" autocomplete="off">
            <div class="form-text">Еднократен. След влизане си генерирайте нови от „Сигурност“.</div>
          </div>
          <button class="btn btn-brand w-100 py-2">Потвърди</button>
        </form>
        <div class="text-center mt-3">
          <a href="#" id="toggleRec" class="small">Нямам телефона — ще ползвам код за възстановяване</a>
        </div>
        <div id="msg" class="mt-3"></div>
      </div>
    </div>
    <p class="text-center text-white-50 small mt-4">
      Прозорецът е {PREAUTH_TTL // 60} минути. <a class="text-white" href="{ADMIN_PATH_PREFIX}/login">Назад към входа</a>
    </p>
  </div>
</div>
<script>
let useRec = false;
function setMsg(txt, type) {{
  const m = document.getElementById('msg');
  m.className = 'alert alert-' + (type || 'info') + ' small';
  m.textContent = txt;
}}
document.getElementById('toggleRec').addEventListener('click', (e) => {{
  e.preventDefault();
  useRec = !useRec;
  document.getElementById('codeBox').classList.toggle('d-none', useRec);
  document.getElementById('recBox').classList.toggle('d-none', !useRec);
  e.target.textContent = useRec ? 'Обратно към кода от приложението' : 'Нямам телефона — ще ползвам код за възстановяване';
  document.querySelector(useRec ? '[name=recovery_code]' : '[name=code]').focus();
}});
document.getElementById('totpForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  const payload = useRec ? {{recovery_code: d.recovery_code}} : {{code: d.code}};
  setMsg('Проверявам...', 'secondary');
  try {{
    const r = await fetch('{ADMIN_PATH_PREFIX}/api/login/totp', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(payload), credentials: 'include',
    }});
    const j = await r.json();
    if (r.ok) {{ window.location.href = j.next || '{ADMIN_PATH_PREFIX}/dashboard'; return; }}
    setMsg(j.error || 'Грешка', 'danger');
    if (j.next) setTimeout(() => {{ window.location.href = j.next; }}, 2500);
  }} catch (err) {{ setMsg('Мрежова грешка: ' + err, 'danger'); }}
}});
</script>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell("MCP Admin · Втори фактор", body)))


async def _api_login_totp(req: Request):
    gate = _gate(req)
    if gate: return gate
    ip, ua = _req_meta(req)
    pre = _read_preauth(req)
    if not pre:
        return JSONResponse({"error": "Прозорецът за втория фактор изтече. Влезте отново.",
                             "next": f"{ADMIN_PATH_PREFIX}/login"}, status_code=401)
    data = await _json_body(req)
    login = pre["login"]
    au = _load_user_auth(login)
    if not au:
        _preauth_delete(pre["token"])
        return JSONResponse({"error": "User not found"}, status_code=401)
    rem = max(_lockout_remaining(ip), _lockout_remaining(ip, login))
    if rem > 0:
        return JSONResponse({"error": f"Твърде много опити. Изчакайте {max(1, rem // 60)}м."}, status_code=429)
    fail, _ = _recent_failures(ip)

    recovery = (data.get("recovery_code") or "").strip()
    extra: dict = {}
    if recovery:
        ok = _recovery_check(login, recovery)
        factor, reason = "recovery", ("" if ok else "invalid_recovery_code")
    else:
        res = _totp_check(login, (data.get("code") or "").strip())
        ok = bool(res.get("ok"))
        factor, reason = "totp", (res.get("reason") or "")
        if res.get("retry_after"):
            extra["retry_after"] = res["retry_after"]
    if not ok:
        _record_attempt(ip, login, False)
        await _tarpit_delay(fail + 1)
        _audit(login, "totp_fail", "", ip, ua, {"factor": factor, "reason": reason})
        msg = {
            "locked": "Твърде много грешни кодове. Изчакайте няколко минути.",
            "replay": "Този код вече е използван. Изчакайте следващия.",
            "invalid_recovery_code": "Невалиден или вече използван код за възстановяване.",
            "weak_or_missing_pepper": "Сървърът няма MCP_KEY_PEPPER — обърнете се към админа.",
            "decrypt_failed": "Тайната не се дешифрира (сменен MCP_KEY_PEPPER?) — обърнете се към админа.",
        }.get(reason, "Грешен код.")
        return JSONResponse({"error": msg, **extra}, status_code=401)

    _preauth_delete(pre["token"])
    _record_attempt(ip, login, True)
    if factor == "recovery":
        _audit(login, "recovery_code_used", "", ip, ua, {"left": _totp_public(login)["recovery_left"]})
    resp = _complete_login(login, au, pre.get("intent") or {}, ip, ua, second_factor=factor)
    _clear_preauth_cookie(resp)
    return resp


# ─── „Сигурност“: 2FA, парола, сесии (3.3.8) ─────────────────
async def _handle_security_page(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)
    au = _load_user_auth(sess["login"])
    if not au or au.get("setup_pending"):
        return RedirectResponse(f"{ADMIN_PATH_PREFIX}/setup", status_code=302)
    must_enroll = _enrollment_required(sess["login"], au)
    policy_on = _totp_policy_applies(au)
    policy_alert = ""
    if must_enroll:
        policy_alert = """
<div class="alert alert-warning"><i class="bi bi-exclamation-triangle"></i>
  Политиката на сървъра изисква втори фактор за вашия профил. Останалите страници се отключват след записването.</div>"""
    body = f"""
{_nav(sess)}
<div class="container py-4">
  <h2 class="mb-1"><i class="bi bi-shield-lock text-brand"></i> Сигурност</h2>
  <p class="text-muted">Втори фактор, парола и активни сесии на <code>{html.escape(sess['login'])}</code>.</p>
  {policy_alert}
  <div class="row">
    <div class="col-lg-6 mb-4">
      <div class="card shadow-sm h-100">
        <div class="card-header brand-bg"><h5 class="mb-0"><i class="bi bi-phone"></i> Двуфакторна защита (TOTP)</h5></div>
        <div class="card-body">
          <div id="totpStatus" class="mb-3">Зареждам…</div>
          <div class="d-flex flex-wrap gap-2">
            <button class="btn btn-brand btn-sm" id="btnEnroll"><i class="bi bi-qr-code"></i> Включи</button>
            <button class="btn btn-outline-accent btn-sm" id="btnRecovery"><i class="bi bi-life-preserver"></i> Нови кодове за възстановяване</button>
            <button class="btn btn-outline-danger btn-sm" id="btnDisable"><i class="bi bi-shield-x"></i> Изключи</button>
          </div>
          <p class="small text-muted mt-3 mb-0">Google Authenticator, Aegis, 1Password, Bitwarden — всяко приложение с TOTP (RFC 6238).
          {'Политиката не позволява изключване за този профил; повторното записване заменя тайната.' if policy_on else ''}</p>
        </div>
      </div>
    </div>
    <div class="col-lg-6 mb-4">
      <div class="card shadow-sm h-100">
        <div class="card-header brand-bg"><h5 class="mb-0"><i class="bi bi-key-fill"></i> Смяна на парола</h5></div>
        <div class="card-body">
          <form id="pwForm" autocomplete="off">
            <div class="mb-2"><label class="form-label small">Текуща парола</label>
              <input name="current" type="password" class="form-control" required autocomplete="current-password"></div>
            <div class="mb-2"><label class="form-label small">Нова парола (мин. 12 символа)</label>
              <input name="new" type="password" class="form-control" required minlength="12" autocomplete="new-password"></div>
            <div class="mb-2"><label class="form-label small">Повторете новата</label>
              <input name="new2" type="password" class="form-control" required minlength="12" autocomplete="new-password"></div>
            <div class="mb-3 d-none" id="pwCodeBox"><label class="form-label small">Код от приложението</label>
              <input name="code" type="text" class="form-control" inputmode="numeric" maxlength="7" autocomplete="one-time-code"></div>
            <button class="btn btn-brand btn-sm"><i class="bi bi-save"></i> Смени</button>
            <span class="small text-muted ms-2">Останалите ви сесии се затварят.</span>
          </form>
          <div id="pwMsg" class="mt-3"></div>
        </div>
      </div>
    </div>
  </div>
  <div class="card shadow-sm mb-4">
    <div class="card-body">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <h5 class="fw-bold mb-0"><i class="bi bi-laptop"></i> Активни сесии</h5>
        <button class="btn btn-outline-danger btn-sm" id="btnRevoke"><i class="bi bi-x-circle"></i> Затвори останалите</button>
      </div>
      <div id="sessList">Зареждам…</div>
    </div>
  </div>
</div>

<!-- Enrol modal: парола → QR → код → кодове за възстановяване -->
<div class="modal fade" id="enrollModal" tabindex="-1" data-bs-backdrop="static">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header brand-bg text-white">
        <h5 class="modal-title"><i class="bi bi-qr-code"></i> Включване на втори фактор</h5>
        <button class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div id="enStep1">
          <p class="small text-muted">Потвърдете с текущата си парола.</p>
          <input id="enPw" type="password" class="form-control" autocomplete="current-password" placeholder="Парола">
        </div>
        <div id="enStep2" class="d-none">
          <p class="small text-muted mb-2">Сканирайте QR кода или въведете тайната ръчно, после напишете кода, който приложението показва.</p>
          <div class="text-center mb-2"><img id="enQr" alt="QR" style="max-width:220px;"></div>
          <code class="apikey mb-2" id="enSecret"></code>
          <input id="enCode" type="text" class="form-control form-control-lg text-center" inputmode="numeric" maxlength="7"
                 autocomplete="one-time-code" placeholder="6-цифрен код">
        </div>
        <div id="enStep3" class="d-none">
          <div class="alert alert-warning small mb-2"><strong>Кодове за възстановяване — показват се ВЕДНЪЖ.</strong>
            Всеки влиза еднократно вместо кода от приложението. Пазете ги извън телефона.</div>
          <code class="apikey" id="enRecovery" style="white-space:pre-line;"></code>
        </div>
        <div id="enMsg" class="mt-3"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-secondary" data-bs-dismiss="modal" id="enCancel">Откажи</button>
        <button class="btn btn-brand" id="enNext">Напред</button>
      </div>
    </div>
  </div>
</div>

<!-- Rechallenge modal (парола + код) за изключване / нови кодове -->
<div class="modal fade" id="rcModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header brand-bg text-white">
        <h5 class="modal-title" id="rcTitle">Потвърждение</h5>
        <button class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <p class="small text-muted" id="rcText"></p>
        <input id="rcPw" type="password" class="form-control mb-2" autocomplete="current-password" placeholder="Парола">
        <input id="rcCode" type="text" class="form-control text-center" inputmode="numeric" maxlength="7"
               autocomplete="one-time-code" placeholder="Код от приложението">
        <code class="apikey mt-3 d-none" id="rcOut" style="white-space:pre-line;"></code>
        <div id="rcMsg" class="mt-3"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-outline-secondary" data-bs-dismiss="modal">Затвори</button>
        <button class="btn btn-brand" id="rcGo">Потвърди</button>
      </div>
    </div>
  </div>
</div>

<script>
const PATH = '{ADMIN_PATH_PREFIX}';
let _csrf = null;
async function csrf() {{
  if (_csrf) return _csrf;
  const j = await fetch(PATH + '/api/csrf', {{credentials:'include'}}).then(r => r.json());
  _csrf = j.token; return _csrf;
}}
function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}
function say(id, txt, type) {{
  const m = document.getElementById(id);
  m.className = txt ? ('alert alert-' + (type || 'info') + ' small') : '';
  m.textContent = txt || '';
}}
async function api(path, body) {{
  const t = await csrf();
  const r = await fetch(PATH + path, {{
    method: 'POST', credentials: 'include',
    headers: {{'Content-Type': 'application/json', 'X-CSRF-Token': t}},
    body: JSON.stringify(body || {{}}),
  }});
  const j = await r.json().catch(() => ({{}}));
  return [r, j];
}}

let status = {{}};
async function loadStatus() {{
  status = await fetch(PATH + '/api/totp/status', {{credentials:'include'}}).then(r => r.json());
  const box = document.getElementById('totpStatus');
  if (!status.pepper_ready) {{
    box.innerHTML = '<div class="alert alert-danger small mb-0"><strong>MCP_KEY_PEPPER</strong> не е зададен на сървъра (≥32 знака). Без него тайната няма с какво да се криптира и 2FA не може да се включи.</div>';
  }} else if (status.enrolled) {{
    box.innerHTML = '<span class="badge bg-success fs-6">включен</span> <span class="small text-muted ms-2">от ' + esc((status.enrolled_at||'').slice(0,16).replace('T',' ')) +
      (status.last_used ? ' · последно ' + esc(status.last_used.slice(0,16).replace('T',' ')) : ' · още не е ползван') +
      ' · кодове за възстановяване: <strong>' + status.recovery_left + '</strong></span>' +
      (status.recovery_left === 0 ? '<div class="text-danger small mt-1">Нямате кодове за възстановяване — генерирайте нови.</div>' : '');
  }} else {{
    box.innerHTML = '<span class="badge bg-secondary fs-6">изключен</span> <span class="small text-muted ms-2">Входът е само с парола.</span>';
  }}
  const en = !!status.enrolled, ok = !!status.pepper_ready;
  document.getElementById('btnEnroll').disabled = !ok;
  document.getElementById('btnEnroll').innerHTML = en ? '<i class="bi bi-arrow-repeat"></i> Запиши наново' : '<i class="bi bi-qr-code"></i> Включи';
  document.getElementById('btnRecovery').classList.toggle('d-none', !en);
  document.getElementById('btnDisable').classList.toggle('d-none', !en || {str(policy_on).lower()});
  document.getElementById('pwCodeBox').classList.toggle('d-none', !en);
}}

// ── enrol ──
let enStep = 1;
const enModal = () => bootstrap.Modal.getOrCreateInstance(document.getElementById('enrollModal'));
document.getElementById('btnEnroll').addEventListener('click', () => {{
  enStep = 1;
  for (const s of [1,2,3]) document.getElementById('enStep' + s).classList.toggle('d-none', s !== 1);
  document.getElementById('enPw').value = ''; document.getElementById('enCode').value = '';
  document.getElementById('enNext').textContent = 'Напред'; document.getElementById('enCancel').classList.remove('d-none');
  say('enMsg', '');
  enModal().show();
  setTimeout(() => document.getElementById('enPw').focus(), 300);
}});
document.getElementById('enNext').addEventListener('click', async () => {{
  say('enMsg', '');
  if (enStep === 1) {{
    const [r, j] = await api('/api/totp/enroll', {{password: document.getElementById('enPw').value}});
    if (!r.ok) {{ say('enMsg', j.error || 'Грешка', 'danger'); return; }}
    document.getElementById('enQr').src = j.qr_svg || '';
    document.getElementById('enQr').classList.toggle('d-none', !j.qr_svg);
    document.getElementById('enSecret').textContent = j.secret;
    enStep = 2;
    document.getElementById('enStep1').classList.add('d-none'); document.getElementById('enStep2').classList.remove('d-none');
    document.getElementById('enNext').textContent = 'Потвърди кода';
    setTimeout(() => document.getElementById('enCode').focus(), 100);
  }} else if (enStep === 2) {{
    const [r, j] = await api('/api/totp/confirm', {{code: document.getElementById('enCode').value}});
    if (!r.ok) {{ say('enMsg', j.error || 'Грешка', 'danger'); return; }}
    document.getElementById('enRecovery').textContent = (j.recovery_codes || []).join('\\n');
    enStep = 3;
    document.getElementById('enStep2').classList.add('d-none'); document.getElementById('enStep3').classList.remove('d-none');
    document.getElementById('enNext').textContent = 'Записах ги';
    document.getElementById('enCancel').classList.add('d-none');
  }} else {{
    enModal().hide();
    loadStatus();
    {"window.location.href = PATH + '/dashboard';" if must_enroll else ""}
  }}
}});

// ── rechallenge (disable / recovery) ──
let rcAction = null;
const rcModal = () => bootstrap.Modal.getOrCreateInstance(document.getElementById('rcModal'));
function openRc(action, title, text) {{
  rcAction = action;
  document.getElementById('rcTitle').textContent = title;
  document.getElementById('rcText').textContent = text;
  document.getElementById('rcPw').value = ''; document.getElementById('rcCode').value = '';
  document.getElementById('rcOut').classList.add('d-none'); document.getElementById('rcOut').textContent = '';
  document.getElementById('rcGo').classList.remove('d-none');
  say('rcMsg', '');
  rcModal().show();
  setTimeout(() => document.getElementById('rcPw').focus(), 300);
}}
document.getElementById('btnDisable').addEventListener('click', () =>
  openRc('/api/totp/disable', 'Изключване на втория фактор', 'Входът ще е само с парола. Потвърдете с паролата и текущия код.'));
document.getElementById('btnRecovery').addEventListener('click', () =>
  openRc('/api/totp/recovery', 'Нови кодове за възстановяване', 'Старите кодове спират да важат. Потвърдете с паролата и текущия код.'));
document.getElementById('rcGo').addEventListener('click', async () => {{
  say('rcMsg', '');
  const [r, j] = await api(rcAction, {{password: document.getElementById('rcPw').value, code: document.getElementById('rcCode').value}});
  if (!r.ok) {{ say('rcMsg', j.error || 'Грешка', 'danger'); return; }}
  if (j.recovery_codes) {{
    document.getElementById('rcOut').textContent = j.recovery_codes.join('\\n');
    document.getElementById('rcOut').classList.remove('d-none');
    document.getElementById('rcGo').classList.add('d-none');
    say('rcMsg', 'Показват се веднъж — запишете ги.', 'warning');
  }} else {{
    rcModal().hide();
  }}
  loadStatus();
}});

// ── password ──
document.getElementById('pwForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  if (d.new !== d.new2) {{ say('pwMsg', 'Паролите не съвпадат', 'danger'); return; }}
  const [r, j] = await api('/api/password', {{current: d.current, new: d.new, code: d.code}});
  if (r.ok) {{ say('pwMsg', 'Паролата е сменена. Затворени сесии: ' + (j.sessions_revoked || 0), 'success'); e.target.reset(); loadSessions(); }}
  else say('pwMsg', j.error || 'Грешка', 'danger');
}});

// ── sessions ──
async function loadSessions() {{
  const j = await fetch(PATH + '/api/sessions', {{credentials:'include'}}).then(r => r.json());
  let rows = '';
  for (const s of (j.sessions || [])) {{
    rows += `<tr class="${{s.current ? 'table-success' : ''}}">
      <td>${{s.current ? '<span class="badge bg-success">тази</span>' : ''}}</td>
      <td><code>${{esc(s.ip)}}</code></td>
      <td class="small text-muted text-truncate" style="max-width:320px" title="${{esc(s.ua)}}">${{esc(s.ua)}}</td>
      <td class="small">${{esc(s.created)}}</td><td class="small">${{esc(s.expires)}}</td></tr>`;
  }}
  document.getElementById('sessList').innerHTML =
    `<div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead class="small text-muted"><tr><th></th><th>IP</th><th>Браузър</th><th>Създадена</th><th>Изтича</th></tr></thead><tbody>${{rows}}</tbody></table></div>`;
}}
document.getElementById('btnRevoke').addEventListener('click', async () => {{
  if (!confirm('Затвори всички други сесии на профила?')) return;
  const [r, j] = await api('/api/sessions/revoke-others', {{}});
  if (r.ok) loadSessions(); else alert(j.error || 'Грешка');
}});

loadStatus(); loadSessions();
{"document.getElementById('btnEnroll').click();" if must_enroll else ""}
</script>
"""
    return _apply_sec_headers(HTMLResponse(_html_shell("MCP Admin · Сигурност", body)))


async def _api_totp_status(req: Request):
    sess, err = _api_session(req)
    if err: return err
    return _apply_sec_headers(JSONResponse(_totp_public(sess["login"])))


async def _api_totp_enroll(req: Request):
    sess, err = _api_session(req, csrf=True)
    if err: return err
    ip, ua = _req_meta(req)
    data = await _json_body(req)
    if not _password_ok(sess["login"], data.get("password") or ""):
        _audit(sess["login"], "totp_enroll_denied", "", ip, ua)
        return JSONResponse({"error": "Грешна парола"}, status_code=403)
    out = _totp_begin_enroll(sess["login"])
    if "error" in out:
        return JSONResponse({"error": out["error"]}, status_code=400)
    _audit(sess["login"], "totp_enroll_begin", "", ip, ua)
    return _apply_sec_headers(JSONResponse(out))


async def _api_totp_confirm(req: Request):
    sess, err = _api_session(req, csrf=True)
    if err: return err
    ip, ua = _req_meta(req)
    data = await _json_body(req)
    out = _totp_confirm_enroll(sess["login"], data.get("code") or "")
    if "error" in out:
        _audit(sess["login"], "totp_enroll_confirm_fail", "", ip, ua)
        return JSONResponse({"error": out["error"]}, status_code=400)
    _audit(sess["login"], "totp_enroll", "", ip, ua)
    return _apply_sec_headers(JSONResponse(out))


async def _rechallenge(req: Request, sess: dict, data: dict) -> Response | None:
    """Парола + текущ код — за изключване и за нови кодове. None = минава."""
    ip, ua = _req_meta(req)
    if not _password_ok(sess["login"], data.get("password") or ""):
        _audit(sess["login"], "totp_rechallenge_denied", "", ip, ua, {"reason": "password"})
        return JSONResponse({"error": "Грешна парола"}, status_code=403)
    if not _totp_enrolled(sess["login"]):
        return JSONResponse({"error": "Вторият фактор не е включен"}, status_code=400)
    res = _totp_check(sess["login"], data.get("code") or "")
    if not res.get("ok"):
        _audit(sess["login"], "totp_rechallenge_denied", "", ip, ua, {"reason": res.get("reason")})
        return JSONResponse({"error": "Грешен код" if res.get("reason") != "locked"
                             else "Твърде много грешни кодове. Изчакайте няколко минути."}, status_code=401)
    return None


async def _api_totp_disable(req: Request):
    sess, err = _api_session(req, csrf=True)
    if err: return err
    ip, ua = _req_meta(req)
    au = _load_user_auth(sess["login"]) or {}
    if _totp_policy_applies(au):
        return JSONResponse({"error": "Политиката на сървъра изисква втори фактор за този профил."}, status_code=403)
    data = await _json_body(req)
    denied = await _rechallenge(req, sess, data)
    if denied: return denied
    _totp_remove(sess["login"])
    _audit(sess["login"], "totp_disable", "", ip, ua)
    return _apply_sec_headers(JSONResponse({"ok": True}))


async def _api_totp_recovery(req: Request):
    sess, err = _api_session(req, csrf=True)
    if err: return err
    ip, ua = _req_meta(req)
    data = await _json_body(req)
    denied = await _rechallenge(req, sess, data)
    if denied: return denied
    codes = _recovery_new(sess["login"])
    _audit(sess["login"], "recovery_codes_regenerated", "", ip, ua)
    return _apply_sec_headers(JSONResponse({"ok": True, "recovery_codes": codes}))


async def _api_password(req: Request):
    sess, err = _api_session(req, csrf=True)
    if err: return err
    ip, ua = _req_meta(req)
    data = await _json_body(req)
    login = sess["login"]
    current = data.get("current") or ""
    new = (data.get("new") or "").strip()
    if not _password_ok(login, current):
        _audit(login, "password_change_denied", "", ip, ua, {"reason": "password"})
        return JSONResponse({"error": "Грешна текуща парола"}, status_code=403)
    if len(new) < 12:
        return JSONResponse({"error": "Новата парола трябва да е минимум 12 символа"}, status_code=400)
    if new == current:
        return JSONResponse({"error": "Новата парола съвпада с текущата"}, status_code=400)
    if _totp_enrolled(login):
        res = _totp_check(login, data.get("code") or "")
        if not res.get("ok"):
            _audit(login, "password_change_denied", "", ip, ua, {"reason": res.get("reason")})
            return JSONResponse({"error": "Грешен код от приложението"}, status_code=401)
    au = _load_user_auth(login)
    if not au:
        return JSONResponse({"error": "User not found"}, status_code=404)
    au["password_hash"] = _hash_password(new)
    au["password_updated_at"] = _now()
    _save_user_auth(login, au)
    revoked = _delete_other_sessions(login, sess["sid"])
    _audit(login, "password_change", "", ip, ua, {"sessions_revoked": revoked})
    return _apply_sec_headers(JSONResponse({"ok": True, "sessions_revoked": revoked}))


async def _api_sessions(req: Request):
    sess, err = _api_session(req)
    if err: return err
    out = []
    for s in _list_sessions(sess["login"]):
        out.append({
            "current": s["sid"] == sess["sid"],
            "ip": s.get("ip") or "", "ua": s.get("ua") or "",
            "admin": bool(s.get("is_admin")),
            "created": datetime.fromtimestamp(s["created_at"]).strftime("%Y-%m-%d %H:%M"),
            "expires": datetime.fromtimestamp(s["expires_at"]).strftime("%Y-%m-%d %H:%M"),
        })
    return _apply_sec_headers(JSONResponse({"sessions": out}))


async def _api_sessions_revoke_others(req: Request):
    sess, err = _api_session(req, csrf=True)
    if err: return err
    ip, ua = _req_meta(req)
    n = _delete_other_sessions(sess["login"], sess["sid"])
    _audit(sess["login"], "sessions_revoke_others", "", ip, ua, {"revoked": n})
    return _apply_sec_headers(JSONResponse({"ok": True, "revoked": n}))


async def _api_user_totp_reset(req: Request):
    """Админът нулира втория фактор на потребител (загубен телефон, без кодове
    за възстановяване). Сесиите на потребителя не се пипат."""
    sess, err = _api_session(req, csrf=True, admin=True)
    if err: return err
    ip, ua = _req_meta(req)
    target = _sanitize_login(req.path_params.get("login", ""))
    if not _load_user_auth(target):
        return JSONResponse({"error": "Not found"}, status_code=404)
    if not _totp_enrolled(target) and not (_totp_load(target) or {}).get("pending_secret_enc"):
        return JSONResponse({"error": "Потребителят няма включен втори фактор"}, status_code=400)
    _totp_remove(target)
    _audit(sess["login"], "user_totp_reset", target, ip, ua)
    return _apply_sec_headers(JSONResponse({"ok": True}))


async def _handle_logout(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if sess:
        _delete_session(sess["sid"])
        _audit(sess["login"], "logout", "", _client_ip(req), req.headers.get("user-agent",""))
    resp = RedirectResponse(f"{ADMIN_PATH_PREFIX}/login", status_code=302)
    _clear_session_cookie(resp)
    return resp


async def _handle_robots(req: Request):
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# ─── Политика над всички маршрути (3.3.8) ────────────────────
_POLICY_EXEMPT = {
    "", "/", "/login", "/totp", "/setup", "/logout", "/robots.txt", "/security",
    "/api/login/mcp", "/api/login/odoo", "/api/login/totp", "/api/setup-password",
    "/api/csrf", "/api/password", "/api/sessions", "/api/sessions/revoke-others",
}


def _policy_exempt(path: str) -> bool:
    rel = path[len(ADMIN_PATH_PREFIX):] if ADMIN_PATH_PREFIX and path.startswith(ADMIN_PATH_PREFIX) else path
    return rel in _POLICY_EXEMPT or rel.startswith("/api/totp/")


class _PolicyMiddleware:
    """Един гард за ВСИЧКИ маршрути под префикса, включително разширенията
    (backups, filestore), които admin_ui не вижда при писане:
    - сесия с незавършен setup не стига до API-тата и страниците — дотогава
      редeem-нат API key даваше достъп до връзките преди изобщо да има парола;
    - при MCP_ADMIN_REQUIRE_TOTP профил в обхвата без записан фактор вижда само
      „Сигурност“, докато не го запише."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            req = Request(scope)
            path = req.url.path
            if not _policy_exempt(path):
                sess = _read_session(req)
                if sess:
                    au = _load_user_auth(sess["login"]) or {}
                    is_api = path.startswith(f"{ADMIN_PATH_PREFIX}/api/")
                    resp = None
                    if au.get("setup_pending"):
                        resp = (JSONResponse({"error": "setup_required", "next": f"{ADMIN_PATH_PREFIX}/setup"}, status_code=403)
                                if is_api else RedirectResponse(f"{ADMIN_PATH_PREFIX}/setup", status_code=302))
                    elif _enrollment_required(sess["login"], au):
                        resp = (JSONResponse({"error": "totp_enrollment_required", "next": f"{ADMIN_PATH_PREFIX}/security"}, status_code=403)
                                if is_api else RedirectResponse(f"{ADMIN_PATH_PREFIX}/security?enroll=1", status_code=302))
                    if resp is not None:
                        await _apply_sec_headers(resp)(scope, receive, send)
                        return
        await self.app(scope, receive, send)


# ─── Route registration ──────────────────────────────────────
def get_asgi_app():
    """Return a Starlette sub-app with all admin routes, or None if disabled."""
    routes = get_routes()
    if not routes:
        return None
    from starlette.applications import Starlette
    return _PolicyMiddleware(Starlette(routes=routes))


def path_matches(path: str) -> bool:
    """Constant-time check if path is under admin prefix."""
    if not ADMIN_ENABLED:
        return False
    p = ADMIN_PATH_PREFIX
    if path == p or path.startswith(p + "/"):
        return True
    return False


def get_routes() -> list:
    if not ADMIN_ENABLED:
        _logger.info("Admin UI disabled (MCP_ADMIN_PATH_PREFIX empty)")
        return []
    if not _ITSDANGEROUS_AVAILABLE:
        _logger.error("itsdangerous missing — admin UI will not be registered")
        return []
    if SESSION_SECRET == _INSECURE_SECRET:
        # 3.3.8: дотук конзолата се качваше с публичен подпис на бисквитката —
        # всеки, който знае низа, си подписва сесия. Fail-closed.
        _logger.error("MCP_SECRET_TOKEN / MCP_ADMIN_SESSION_SECRET not set — "
                      "admin UI will not be registered (session cookies would be signed with a public default)")
        return []
    if not BOOTSTRAP_ADMIN:
        _logger.warning("MCP_BOOTSTRAP_ADMIN not set — no user will be auto-promoted to admin")
    if REQUIRE_TOTP and REQUIRE_TOTP not in ("admins", "all"):
        _logger.error("MCP_ADMIN_REQUIRE_TOTP=%r is not 'admins' or 'all' — policy ignored", REQUIRE_TOTP)
    if REQUIRE_TOTP in ("admins", "all") and not _totp_pepper_ok():
        _logger.error("MCP_ADMIN_REQUIRE_TOTP=%s but MCP_KEY_PEPPER is unset/weak — users in scope "
                      "cannot enrol and will be held on /security until the pepper is set", REQUIRE_TOTP)
    p = ADMIN_PATH_PREFIX
    _logger.info("Admin UI mounted at %s (admin: %s, knock: %s, require_totp: %s)",
                 p, BOOTSTRAP_ADMIN or "(none)", "enabled" if KNOCK_TOKEN else "disabled",
                 REQUIRE_TOTP or "off")
    return [
        Route(f"{p}", _handle_root),
        Route(f"{p}/", _handle_root),
        Route(f"{p}/login", _handle_login_page),
        Route(f"{p}/totp", _handle_totp_page),
        Route(f"{p}/setup", _handle_setup_page),
        Route(f"{p}/dashboard", _handle_dashboard),
        Route(f"{p}/connections", _handle_connections_page),
        Route(f"{p}/security", _handle_security_page),
        Route(f"{p}/users", _handle_users_page),
        Route(f"{p}/logout", _handle_logout),
        Route(f"{p}/robots.txt", _handle_robots),
        Route(f"{p}/api/login/mcp", _api_login_mcp, methods=["POST"]),
        Route(f"{p}/api/login/odoo", _api_login_odoo, methods=["POST"]),
        Route(f"{p}/api/login/totp", _api_login_totp, methods=["POST"]),
        Route(f"{p}/api/setup-password", _api_setup_password, methods=["POST"]),
        Route(f"{p}/api/csrf", _api_csrf, methods=["GET"]),
        Route(f"{p}/api/totp/status", _api_totp_status, methods=["GET"]),
        Route(f"{p}/api/totp/enroll", _api_totp_enroll, methods=["POST"]),
        Route(f"{p}/api/totp/confirm", _api_totp_confirm, methods=["POST"]),
        Route(f"{p}/api/totp/disable", _api_totp_disable, methods=["POST"]),
        Route(f"{p}/api/totp/recovery", _api_totp_recovery, methods=["POST"]),
        Route(f"{p}/api/password", _api_password, methods=["POST"]),
        Route(f"{p}/api/sessions", _api_sessions, methods=["GET"]),
        Route(f"{p}/api/sessions/revoke-others", _api_sessions_revoke_others, methods=["POST"]),
        Route(f"{p}/api/connections", _api_connections, methods=["GET","POST"]),
        Route(f"{p}/api/connections/import", _api_connections_import, methods=["POST"]),
        Route(f"{p}/api/connections/{{alias}}", _api_connection_crud, methods=["GET","PUT","DELETE"]),
        Route(f"{p}/api/users", _api_users, methods=["GET","POST"]),
        Route(f"{p}/api/users/{{login}}/genkey", _api_user_genkey, methods=["POST"]),
        Route(f"{p}/api/users/{{login}}/totp-reset", _api_user_totp_reset, methods=["POST"]),
    ] + _extension_routes()


def _extension_routes() -> list:
    """Append routes from optional sibling modules (backup manager, filestore)."""
    out: list = []
    for mod_name in ("admin_backup", "admin_filestore"):
        try:
            mod = __import__(mod_name)
            if hasattr(mod, "get_routes"):
                r = mod.get_routes() or []
                out.extend(r)
                _logger.info("mounted %d routes from %s", len(r), mod_name)
        except ImportError:
            continue
        except Exception as exc:
            _logger.error("failed to load %s: %s", mod_name, exc)
    return out
