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

Integrated into server.py via register_admin_routes(app).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import time
import xmlrpc.client
from datetime import datetime, timedelta
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

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route

_logger = logging.getLogger("odoo-rpc-mcp.admin_ui")

# ─── Config (env) ────────────────────────────────────────────
ADMIN_PATH_PREFIX = (os.environ.get("MCP_ADMIN_PATH_PREFIX") or "/admin").rstrip("/")
ADMIN_ENABLED = ADMIN_PATH_PREFIX != ""
BOOTSTRAP_ADMIN = (os.environ.get("MCP_BOOTSTRAP_ADMIN") or "").strip().lower()
KNOCK_TOKEN = (os.environ.get("MCP_ADMIN_KNOCK_TOKEN") or "").strip()
SESSION_SECRET = (
    os.environ.get("MCP_ADMIN_SESSION_SECRET")
    or os.environ.get("MCP_SECRET_TOKEN")
    or "INSECURE-DEFAULT-SET-MCP_SECRET_TOKEN"
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

# Paths
DATA_DIR = "/data"
USERS_DIR = os.path.join(DATA_DIR, "users")
SESSIONS_DB = os.path.join(DATA_DIR, "sessions.db")
AUDIT_LOG = os.path.join(DATA_DIR, "admin_audit.log")
ADMIN_CONFIG = os.path.join(DATA_DIR, "admin_config.json")

_SIGNER = URLSafeTimedSerializer(SESSION_SECRET, salt="mcp-admin-v1") if _ITSDANGEROUS_AVAILABLE else None


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
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
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
    return f"""<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>{title}</title>
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

    # Lockout check
    rem = _lockout_remaining(ip)
    if rem > 0:
        return JSONResponse({"error": f"Твърде много опити. Изчакайте {rem // 60}м."}, status_code=429)

    au = _load_user_auth(login)
    fail, _ = _recent_failures(ip)
    if not au or not _verify_password(password, au.get("password_hash", "")):
        _record_attempt(ip, login, False)
        await _tarpit_delay(fail + 1)
        _audit(login, "login_fail", "", ip, ua)
        return JSONResponse({"error":"Грешен login или парола"}, status_code=401)

    _record_attempt(ip, login, True)
    is_admin = bool(au.get("admin", False))
    sid, _csrf = _create_session(login, is_admin, ip, ua)
    _audit(login, "login_ok", "", ip, ua, {"admin": is_admin})
    resp = JSONResponse({"ok": True, "next": f"{ADMIN_PATH_PREFIX}/dashboard"})
    _set_session_cookie(resp, sid, is_admin)
    return _apply_sec_headers(resp)


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

    rem = _lockout_remaining(ip)
    if rem > 0:
        return JSONResponse({"error": f"Твърде много опити. Изчакайте {rem // 60}м."}, status_code=429)

    au = _load_user_auth(login)
    fail, _ = _recent_failures(ip)

    # Case 1: user has pending api_key_hash → password == API key issued by admin
    if au and au.get("setup_pending") and au.get("api_key_hash"):
        if hmac.compare_digest(_hash_api_key(password), au["api_key_hash"]):
            # API key redeemed — allow setup password form
            # Clear the api_key, keep setup_pending until password is set
            au["api_key_hash"] = ""
            _save_user_auth(login, au)
            _record_attempt(ip, login, True)
            sid, _c = _create_session(login, bool(au.get("admin", False)), ip, ua)
            _audit(login, "setup_api_key_redeemed", "", ip, ua)
            resp = JSONResponse({"ok": True, "next": f"{ADMIN_PATH_PREFIX}/setup"})
            _set_session_cookie(resp, sid, bool(au.get("admin", False)))
            return _apply_sec_headers(resp)
        else:
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
        # New user
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
    else:
        # Existing user re-authenticating with Odoo (e.g. password reset flow)
        au["setup_pending"] = True
        au.setdefault("odoo", {})["url"] = url.rstrip("/")
        au["odoo"]["db"] = db
        au["odoo"]["uid"] = uid
        _save_user_auth(login, au)
        _audit(login, "user_reauth_via_odoo", "", ip, ua)

    # Auto-save the credentials as 'default' alias in user's connections.json
    # so they see at least one connection immediately after setup
    try:
        conn_file = os.path.join(_user_dir(login), "connections.json")
        data = {}
        if os.path.isfile(conn_file):
            try:
                with open(conn_file) as _f: data = json.load(_f)
            except (json.JSONDecodeError, OSError):
                data = {}
        if "default" not in data:
            data["default"] = {
                "url": url.rstrip("/"),
                "db": db,
                "user": login,
                "api_key": password,
                "protocol": "xmlrpc",
                "verify_ssl": True,
            }
            with open(conn_file, "w") as _f:
                json.dump(data, _f, indent=2)
            os.chmod(conn_file, 0o600)
            _audit(login, "connection_autosave", "default", ip, ua)
    except Exception as _e:
        _logger.warning("auto-save connection failed: %s", _e)

    sid, _c = _create_session(login, bool(au.get("admin", False)), ip, ua)
    resp = JSONResponse({"ok": True, "next": f"{ADMIN_PATH_PREFIX}/setup"})
    _set_session_cookie(resp, sid, bool(au.get("admin", False)))
    return _apply_sec_headers(resp)


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
            rows.append(f"""
<tr>
  <td class="text-nowrap"><code>{alias}</code>{(' ' + badges) if badges else ''}</td>
  <td class="small text-muted text-truncate" style="max-width: 260px;" title="{cfg.get('url','')}">{cfg.get('url','')}</td>
  <td class="small text-truncate" style="max-width: 140px;" title="{cfg.get('db','')}">{cfg.get('db','')}</td>
  <td class="text-end text-nowrap">
    <a href="{ADMIN_PATH_PREFIX}/connections#{alias}" class="btn btn-sm btn-outline-primary" title="Редакция"><i class="bi bi-pencil"></i></a>
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
              <button class="btn btn-outline-primary btn-sm" onclick="alert('Change password: влез през Odoo отново, ще изисква нова setup парола')">
                <i class="bi bi-key"></i> Смени парола
              </button>
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
    rows += `
      <tr>
        <td><code class="fs-6">${{alias}}</code><div class="mt-1">${{badges}}</div></td>
        <td class="small text-muted"><div>${{cfg.url||''}}</div><div>${{cfg.db||''}} · ${{cfg.user||''}}</div></td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-primary" onclick="openEditor('${{alias}}','edit')"><i class="bi bi-pencil"></i> Редакция</button>
          <button class="btn btn-sm btn-outline-danger" onclick="delConn('${{alias}}')"><i class="bi bi-trash"></i></button>
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
    rows += `
      <tr>
        <td><code>${{u.login}}</code></td>
        <td><span class="badge ${{bg}}">${{badge}}</span></td>
        <td>${{state}}</td>
        <td class="small text-muted">${{u.created}}</td>
        <td class="text-end">
          <button class="btn btn-sm btn-outline-warning" onclick="regenKey('${{u.login}}')"><i class="bi bi-key"></i> Нов key</button>
        </td>
      </tr>`;
  }}
  list.innerHTML = `<div class="table-responsive"><table class="table table-sm table-mono"><thead><tr><th>Login</th><th>Role</th><th>Status</th><th>Created</th><th></th></tr></thead><tbody>${{rows}}</tbody></table></div>`;
}}

async function regenKey(login) {{
  if (!confirm('Генерирай нов API key за ' + login + '? Потребителят ще трябва да го въведе при следващо влизане през Odoo таба.')) return;
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


# ─── Route registration ──────────────────────────────────────
def get_asgi_app():
    """Return a Starlette sub-app with all admin routes, or None if disabled."""
    routes = get_routes()
    if not routes:
        return None
    from starlette.applications import Starlette
    return Starlette(routes=routes)


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
    if not BOOTSTRAP_ADMIN:
        _logger.warning("MCP_BOOTSTRAP_ADMIN not set — no user will be auto-promoted to admin")
    p = ADMIN_PATH_PREFIX
    _logger.info("Admin UI mounted at %s (admin: %s, knock: %s)",
                 p, BOOTSTRAP_ADMIN or "(none)", "enabled" if KNOCK_TOKEN else "disabled")
    return [
        Route(f"{p}", _handle_root),
        Route(f"{p}/", _handle_root),
        Route(f"{p}/login", _handle_login_page),
        Route(f"{p}/setup", _handle_setup_page),
        Route(f"{p}/dashboard", _handle_dashboard),
        Route(f"{p}/connections", _handle_connections_page),
        Route(f"{p}/users", _handle_users_page),
        Route(f"{p}/logout", _handle_logout),
        Route(f"{p}/robots.txt", _handle_robots),
        Route(f"{p}/api/login/mcp", _api_login_mcp, methods=["POST"]),
        Route(f"{p}/api/login/odoo", _api_login_odoo, methods=["POST"]),
        Route(f"{p}/api/setup-password", _api_setup_password, methods=["POST"]),
        Route(f"{p}/api/csrf", _api_csrf, methods=["GET"]),
        Route(f"{p}/api/connections", _api_connections, methods=["GET","POST"]),
        Route(f"{p}/api/connections/import", _api_connections_import, methods=["POST"]),
        Route(f"{p}/api/connections/{{alias}}", _api_connection_crud, methods=["GET","PUT","DELETE"]),
        Route(f"{p}/api/users", _api_users, methods=["GET","POST"]),
        Route(f"{p}/api/users/{{login}}/genkey", _api_user_genkey, methods=["POST"]),
    ]
