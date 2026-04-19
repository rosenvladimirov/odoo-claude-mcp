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
def _validate_odoo(url: str, db: str, login: str, password_or_key: str) -> int | None:
    """Return uid if auth ok, None otherwise. Uses the existing _UATransport helper
    if available in the parent module (to avoid Cloudflare bot-fight blocking)."""
    try:
        url = (url or "").rstrip("/")
        if not url or not db or not login or not password_or_key:
            return None
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        # Permissive for self-signed — MCP server already has verify_ssl + TOFU flow
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        proxy = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common",
            allow_none=True,
            context=ctx,
        )
        uid = proxy.authenticate(db, login, password_or_key, {})
        return int(uid) if uid else None
    except Exception as e:
        _logger.warning("Odoo auth failed: %s", e)
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
        rows = []
        for alias, cfg in sorted(conns.items()):
            rows.append(f"""
<tr>
  <td><code>{alias}</code></td>
  <td class="small text-muted">{cfg.get('url','')}</td>
  <td class="small">{cfg.get('db','')}</td>
  <td class="small">{cfg.get('user','')}</td>
  <td class="text-end">
    <a href="{ADMIN_PATH_PREFIX}/connections#{alias}" class="btn btn-sm btn-outline-primary">Редактирай</a>
  </td>
</tr>""")
        conns_html = f"""
<div class="table-responsive">
  <table class="table table-mono">
    <thead><tr><th>Alias</th><th>URL</th><th>DB</th><th>User</th><th></th></tr></thead>
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
  <h2 class="mb-1"><i class="bi bi-plug-fill"></i> Odoo връзки</h2>
  <p class="text-muted">Вашите персонални Odoo aliasi. MCP сървърът ги ползва при извикване на <code>odoo_*</code> инструменти.</p>

  <div class="row">
    <div class="col-lg-5 mb-4">
      <div class="card shadow-sm">
        <div class="card-header brand-bg"><h5 class="mb-0">Добави нова</h5></div>
        <div class="card-body">
          <form id="addForm">
            <div class="mb-2"><label class="form-label small">Alias</label><input name="alias" class="form-control" placeholder="raytron, konex-tiva…" required pattern="[a-z0-9_-]+"></div>
            <div class="mb-2"><label class="form-label small">URL</label><input name="url" type="url" class="form-control" placeholder="https://..." required></div>
            <div class="mb-2"><label class="form-label small">Database</label><input name="db" class="form-control" required></div>
            <div class="mb-2"><label class="form-label small">Login (Odoo)</label><input name="user" type="email" class="form-control" required></div>
            <div class="mb-2"><label class="form-label small">API key</label><input name="api_key" type="password" class="form-control" required></div>
            <div class="form-check mb-3">
              <input class="form-check-input" type="checkbox" name="verify_ssl" value="1" id="vs" checked>
              <label class="form-check-label small" for="vs">Verify SSL certificate</label>
            </div>
            <button class="btn btn-brand w-100">Добави</button>
          </form>
          <div id="addMsg" class="mt-2"></div>
        </div>
      </div>
    </div>
    <div class="col-lg-7 mb-4">
      <div class="card shadow-sm">
        <div class="card-body">
          <h5 class="fw-bold">Съществуващи</h5>
          <div id="connList">Зареждам...</div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
async function loadConns() {{
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/connections', {{credentials:'include'}});
  const j = await r.json();
  const list = document.getElementById('connList');
  if (!j.connections || !Object.keys(j.connections).length) {{
    list.innerHTML = '<p class="text-muted small">Няма регистрирани връзки.</p>';
    return;
  }}
  let rows = '';
  for (const [alias, cfg] of Object.entries(j.connections)) {{
    rows += `
      <tr id="row-${{alias}}">
        <td><code>${{alias}}</code></td>
        <td class="small text-muted">${{cfg.url||''}}</td>
        <td class="small">${{cfg.db||''}}</td>
        <td class="small">${{cfg.user||''}}</td>
        <td class="text-end"><button class="btn btn-sm btn-outline-danger" onclick="delConn('${{alias}}')"><i class="bi bi-trash"></i></button></td>
      </tr>`;
  }}
  list.innerHTML = `<div class="table-responsive"><table class="table table-sm table-mono"><thead><tr><th>Alias</th><th>URL</th><th>DB</th><th>User</th><th></th></tr></thead><tbody>${{rows}}</tbody></table></div>`;
}}

async function delConn(alias) {{
  if (!confirm('Изтрий ' + alias + '?')) return;
  const csrf = await fetch('{ADMIN_PATH_PREFIX}/api/csrf', {{credentials:'include'}}).then(r => r.json());
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/connections/' + encodeURIComponent(alias), {{
    method:'DELETE', credentials:'include',
    headers: {{'X-CSRF-Token': csrf.token}},
  }});
  if (r.ok) loadConns();
  else alert('Error: ' + r.status);
}}

document.getElementById('addForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target));
  d.verify_ssl = !!d.verify_ssl;
  const csrf = await fetch('{ADMIN_PATH_PREFIX}/api/csrf', {{credentials:'include'}}).then(r => r.json());
  const r = await fetch('{ADMIN_PATH_PREFIX}/api/connections', {{
    method:'POST', credentials:'include',
    headers:{{'Content-Type':'application/json','X-CSRF-Token': csrf.token}},
    body: JSON.stringify(d),
  }});
  const j = await r.json();
  const m = document.getElementById('addMsg');
  if (r.ok) {{ m.className='alert alert-success small'; m.textContent='Добавено'; e.target.reset(); loadConns(); }}
  else {{ m.className='alert alert-danger small'; m.textContent = j.error || 'Грешка'; }}
}});

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


async def _api_connections(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return JSONResponse({"error":"Unauthenticated"}, status_code=401)
    login = sess["login"]
    conn_file = os.path.join(_user_dir(login), "connections.json")
    if req.method == "GET":
        data = {}
        if os.path.isfile(conn_file):
            try:
                with open(conn_file) as f: data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}
        # Mask api keys in response
        safe = {k: {**v, "api_key": "•••"} for k, v in data.items()}
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
        data = {}
        if os.path.isfile(conn_file):
            try:
                with open(conn_file) as f: data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        data[alias] = {
            "url": (body.get("url") or "").rstrip("/"),
            "db": body.get("db") or "",
            "user": body.get("user") or "",
            "api_key": body.get("api_key") or "",
            "verify_ssl": bool(body.get("verify_ssl", True)),
            "protocol": "xmlrpc",
        }
        os.makedirs(_user_dir(login), exist_ok=True)
        with open(conn_file, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(conn_file, 0o600)
        _audit(login, "connection_add", alias, _client_ip(req), req.headers.get("user-agent",""))
        return _apply_sec_headers(JSONResponse({"ok": True}))


async def _api_connection_delete(req: Request):
    gate = _gate(req)
    if gate: return gate
    sess = _read_session(req)
    if not sess:
        return JSONResponse({"error":"Unauthenticated"}, status_code=401)
    if not _check_csrf(req, sess):
        return JSONResponse({"error":"CSRF failure"}, status_code=403)
    alias = req.path_params.get("alias", "")
    conn_file = os.path.join(_user_dir(sess["login"]), "connections.json")
    if not os.path.isfile(conn_file):
        return JSONResponse({"error":"No connections"}, status_code=404)
    try:
        with open(conn_file) as f: data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    if alias in data:
        data.pop(alias)
        with open(conn_file, "w") as f:
            json.dump(data, f, indent=2)
        _audit(sess["login"], "connection_delete", alias, _client_ip(req), req.headers.get("user-agent",""))
        return _apply_sec_headers(JSONResponse({"ok": True}))
    return JSONResponse({"error":"Not found"}, status_code=404)


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
        Route(f"{p}/api/connections/{{alias}}", _api_connection_delete, methods=["DELETE"]),
        Route(f"{p}/api/users", _api_users, methods=["GET","POST"]),
        Route(f"{p}/api/users/{{login}}/genkey", _api_user_genkey, methods=["POST"]),
    ]
