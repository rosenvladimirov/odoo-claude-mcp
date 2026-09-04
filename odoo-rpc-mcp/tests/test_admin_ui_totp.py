"""3.3.8: ревизия на админ конзолата + втори фактор (TOTP).

Кара admin_ui през Starlette TestClient върху временен DATA_DIR. Odoo
проверката е подменена (няма мрежа), tarpit-ът е изключен, часовникът е
замразен, за да са детерминирани стъпките на TOTP (replay guard-ът брои
стъпки, не секунди). Разширенията (backups/filestore) не се монтират.

Всеки тест тук може да падне: пипни _finish_login да издава сесия без код,
махни проверката на api_key_expires, отвори API-тата за setup_pending — и
съответният тест ще го каже.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

import totp_core  # noqa: E402

P = "/admin"
ADMIN = "admin@example.com"
USER = "user@example.com"
PW = "correct-horse-battery"
PW2 = "another-long-password-2"


class _Clock:
    t = 1_800_000_000.0   # 2027-01-15, произволна фиксирана точка


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time, "time", lambda: c.t)
    return c


@pytest.fixture
def ui(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_SECRET_TOKEN", "s" * 48)
    monkeypatch.setenv("MCP_KEY_PEPPER", "p" * 40)
    monkeypatch.setenv("MCP_BOOTSTRAP_ADMIN", ADMIN)
    for k in ("MCP_ADMIN_REQUIRE_TOTP", "MCP_ADMIN_PATH_PREFIX", "MCP_ADMIN_KNOCK_TOKEN",
              "MCP_ADMIN_ALLOWED_IPS", "MCP_ADMIN_SESSION_SECRET"):
        monkeypatch.delenv(k, raising=False)
    import admin_ui
    importlib.reload(admin_ui)
    monkeypatch.setattr(admin_ui, "_validate_odoo",
                        lambda url, db, login, pw: 7 if pw == "odoo-pass" else None)

    async def _no_tarpit(_n):
        return None
    monkeypatch.setattr(admin_ui, "_tarpit_delay", _no_tarpit)
    monkeypatch.setattr(admin_ui, "_extension_routes", lambda: [])
    return admin_ui


def client(ui) -> TestClient:
    # secure=True бисквитките се пращат само по https.
    return TestClient(ui.get_asgi_app(), base_url="https://testserver")


# ── помощници за потока ──────────────────────────────────────

def odoo_login(c, login, pw="odoo-pass"):
    return c.post(f"{P}/api/login/odoo", json={"url": "https://odoo.example", "db": "db",
                                               "login": login, "password": pw})


def mcp_login(c, login, pw):
    return c.post(f"{P}/api/login/mcp", json={"login": login, "password": pw})


def csrf(c) -> str:
    return c.get(f"{P}/api/csrf").json()["token"]


def post(c, path, body=None):
    return c.post(f"{P}{path}", json=body or {}, headers={"X-CSRF-Token": csrf(c)})


def bootstrap(ui, login=ADMIN, pw=PW) -> TestClient:
    """Нов профил през Odoo + зададена парола ⇒ готова сесия."""
    c = client(ui)
    r = odoo_login(c, login)
    assert r.status_code == 200 and r.json()["next"] == f"{P}/setup", r.text
    r = c.post(f"{P}/api/setup-password", json={"password": pw})
    assert r.status_code == 200, r.text
    return c


def code_for(secret: str, clock: _Clock, offset: int = 0) -> str:
    return totp_core.code(secret, int(clock.t) // totp_core.STEP + offset)


def enrol(c, clock, pw=PW):
    r = post(c, "/api/totp/enroll", {"password": pw})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["otpauth_uri"].startswith("otpauth://totp/") and j.get("qr_svg", "").startswith("data:image/svg")
    r = post(c, "/api/totp/confirm", {"code": code_for(j["secret"], clock)})
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 8 and all(len(x) == 11 and x[5] == "-" for x in codes)
    return j["secret"], codes


def totp_login(c, login, pw, secret, clock, offset=1):
    """Парола → totp_required → код. Кодът е за СЛЕДВАЩАТА стъпка по подразбиране,
    защото потвърждаващият код е изгорил текущата (replay guard)."""
    r = mcp_login(c, login, pw)
    assert r.status_code == 200 and r.json().get("totp_required") is True, r.text
    assert r.json()["next"] == f"{P}/totp"
    return c.post(f"{P}/api/login/totp", json={"code": code_for(secret, clock, offset)})


# ── ревизия ──────────────────────────────────────────────────

def test_refuses_to_mount_with_default_session_secret(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MCP_SECRET_TOKEN", raising=False)
    monkeypatch.delenv("MCP_ADMIN_SESSION_SECRET", raising=False)
    monkeypatch.setenv("MCP_BOOTSTRAP_ADMIN", ADMIN)
    import admin_ui
    importlib.reload(admin_ui)
    assert admin_ui.SESSION_SECRET == admin_ui._INSECURE_SECRET
    assert admin_ui.get_routes() == []
    assert admin_ui.get_asgi_app() is None


def test_data_dir_comes_from_env(ui, tmp_path):
    assert ui.DATA_DIR == str(tmp_path)
    assert Path(ui.SESSIONS_DB).parent == tmp_path


def test_first_login_sets_password_and_autosaves_default_connection(ui):
    c = bootstrap(ui)
    assert c.get(f"{P}/dashboard").status_code == 200
    au = ui._load_user_auth(ADMIN)
    assert au["admin"] is True and au["setup_pending"] is False and au["password_hash"]
    conns = ui._load_connections(ADMIN)
    assert conns["default"]["api_key"] == "odoo-pass" and conns["default"]["db"] == "db"
    # пълен вход с MCP парола — без втори фактор ⇒ директна сесия
    c2 = client(ui)
    r = mcp_login(c2, ADMIN, PW)
    assert r.status_code == 200 and "totp_required" not in r.json()
    assert c2.get(f"{P}/api/connections").status_code == 200


def test_setup_pending_session_cannot_reach_api(ui):
    c = client(ui)
    assert odoo_login(c, USER).status_code == 200          # сесия има, парола няма
    r = c.get(f"{P}/api/connections")
    assert r.status_code == 403 and r.json()["error"] == "setup_required"
    r = c.post(f"{P}/api/connections", json={"alias": "x", "url": "https://x", "db": "d", "user": USER},
               headers={"X-CSRF-Token": "irrelevant"})
    assert r.status_code == 403
    assert c.get(f"{P}/dashboard", follow_redirects=False).status_code == 302
    # setup страницата и паролата остават достъпни
    assert c.get(f"{P}/setup").status_code == 200
    assert c.post(f"{P}/api/setup-password", json={"password": PW}).status_code == 200
    assert c.get(f"{P}/api/connections").status_code == 200


def test_expired_one_time_api_key_is_rejected(ui, clock):
    c = bootstrap(ui)
    r = post(c, "/api/users", {"login": USER, "admin": False})
    assert r.status_code == 200
    key = r.json()["api_key"]
    au = ui._load_user_auth(USER)
    assert au["api_key_expires"] > ui._now()
    au["api_key_expires"] = ui._now() - 1                  # срокът е минал
    ui._save_user_auth(USER, au)
    c2 = client(ui)
    r = odoo_login(c2, USER, pw=key)
    assert r.status_code == 401 and "изтекъл" in r.json()["error"]
    # ключът НЕ е изгорен от изтеклия опит — админът вижда състоянието
    assert ui._load_user_auth(USER)["api_key_hash"]
    # свеж ключ минава и изгаря
    r = post(c, f"/api/users/{USER}/genkey")
    key2 = r.json()["api_key"]
    r = odoo_login(client(ui), USER, pw=key2)
    assert r.status_code == 200 and r.json()["next"] == f"{P}/setup"
    assert ui._load_user_auth(USER)["api_key_hash"] == ""


def test_dashboard_escapes_connection_values(ui):
    c = bootstrap(ui)
    conns = ui._load_connections(ADMIN)
    conns["evil"] = {"url": "https://x/<script>alert(1)</script>", "db": "d\"onmouseover=\"x", "user": ADMIN}
    ui._save_connections(ADMIN, conns)
    html_out = c.get(f"{P}/dashboard").text
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out
    assert 'title="d&quot;onmouseover=&quot;x"' in html_out


# ── втори фактор ─────────────────────────────────────────────

def test_enroll_login_replay_recovery_disable(ui, clock):
    c = bootstrap(ui)
    st = c.get(f"{P}/api/totp/status").json()
    assert st == {"enrolled": False, "pending": False, "enrolled_at": None, "last_used": None,
                  "recovery_left": 0, "pepper_ready": True}

    # грешна парола не започва записване
    r = post(c, "/api/totp/enroll", {"password": "wrong"})
    assert r.status_code == 403 and not (ui._totp_load(ADMIN) or {}).get("pending_secret_enc")

    # грешен код не активира — тайната остава pending, входът е още само парола
    r = post(c, "/api/totp/enroll", {"password": PW})
    assert r.status_code == 200
    r = post(c, "/api/totp/confirm", {"code": "000000"})
    assert r.status_code == 400
    assert ui._totp_public(ADMIN) == {**ui._totp_public(ADMIN), "enrolled": False, "pending": True}
    assert mcp_login(client(ui), ADMIN, PW).json().get("totp_required") is None

    secret, codes = enrol(c, clock)
    st = ui._totp_public(ADMIN)
    assert st["enrolled"] and not st["pending"] and st["recovery_left"] == 8
    d = ui._totp_load(ADMIN)
    assert set(d) >= {"secret_enc", "enrolled_at", "algo", "digits", "step", "last_step", "recovery"}
    assert secret not in json.dumps(d)                     # на диска стои само шифърът

    # паролата сама вече не дава сесия
    c2 = client(ui)
    r = mcp_login(c2, ADMIN, PW)
    assert r.json()["totp_required"] is True
    assert c2.get(f"{P}/dashboard", follow_redirects=False).status_code == 302
    assert c2.get(f"{P}/api/connections").status_code == 401
    assert c2.get(f"{P}/totp").status_code == 200          # страницата за кода се вижда с pre-auth

    # грешен код → 401, верният за следващата стъпка → сесия
    r = c2.post(f"{P}/api/login/totp", json={"code": "000000"})
    assert r.status_code == 401
    r = c2.post(f"{P}/api/login/totp", json={"code": code_for(secret, clock, 1)})
    assert r.status_code == 200 and r.json()["next"] == f"{P}/dashboard", r.text
    assert c2.get(f"{P}/dashboard").status_code == 200
    assert c2.get(f"{P}/api/connections").status_code == 200
    # pre-auth редът е еднократен
    r = c2.post(f"{P}/api/login/totp", json={"code": code_for(secret, clock, 1)})
    assert r.status_code == 401 and r.json()["next"] == f"{P}/login"

    # replay: същият код не влиза втори път
    c3 = client(ui)
    mcp_login(c3, ADMIN, PW)
    r = c3.post(f"{P}/api/login/totp", json={"code": code_for(secret, clock, 1)})
    assert r.status_code == 401 and "вече е използван" in r.json()["error"]

    # код за възстановяване — еднократен
    clock.t += 60
    c4 = client(ui)
    mcp_login(c4, ADMIN, PW)
    r = c4.post(f"{P}/api/login/totp", json={"recovery_code": codes[0].upper()})
    assert r.status_code == 200
    assert ui._totp_public(ADMIN)["recovery_left"] == 7
    c5 = client(ui)
    mcp_login(c5, ADMIN, PW)
    r = c5.post(f"{P}/api/login/totp", json={"recovery_code": codes[0]})
    assert r.status_code == 401

    # нови кодове искат парола + код; старите умират
    r = post(c4, "/api/totp/recovery", {"password": PW, "code": code_for(secret, clock)})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 8
    assert ui._totp_public(ADMIN)["recovery_left"] == 8
    c6 = client(ui)
    mcp_login(c6, ADMIN, PW)
    assert c6.post(f"{P}/api/login/totp", json={"recovery_code": codes[1]}).status_code == 401

    # изключване: парола + код; след това паролата пак е достатъчна
    clock.t += 60
    r = post(c4, "/api/totp/disable", {"password": PW, "code": "000000"})
    assert r.status_code == 401
    r = post(c4, "/api/totp/disable", {"password": PW, "code": code_for(secret, clock)})
    assert r.status_code == 200
    assert ui._totp_load(ADMIN) is None
    r = mcp_login(client(ui), ADMIN, PW)
    assert r.status_code == 200 and "totp_required" not in r.json()


def test_totp_login_page_redirects_without_preauth(ui):
    c = client(ui)
    assert c.get(f"{P}/totp", follow_redirects=False).headers["location"] == f"{P}/login"
    r = c.post(f"{P}/api/login/totp", json={"code": "123456"})
    assert r.status_code == 401


def test_odoo_reauth_is_gated_by_totp(ui, clock):
    """Пътят „забравена парола“ (Odoo re-auth) иначе би заобиколил TOTP-а."""
    c = bootstrap(ui)
    secret, _ = enrol(c, clock)
    ui._save_connections(ADMIN, {})                        # за да се види дали autosave чака кода
    c2 = client(ui)
    r = odoo_login(c2, ADMIN)
    assert r.status_code == 200 and r.json()["totp_required"] is True
    au = ui._load_user_auth(ADMIN)
    assert au["setup_pending"] is False                    # флагът чака кода
    assert ui._load_connections(ADMIN) == {}               # и autosave-ът чака
    assert c2.get(f"{P}/setup", follow_redirects=False).status_code == 302
    r = c2.post(f"{P}/api/login/totp", json={"code": code_for(secret, clock, 1)})
    assert r.status_code == 200 and r.json()["next"] == f"{P}/setup"
    assert ui._load_user_auth(ADMIN)["setup_pending"] is True
    assert c2.post(f"{P}/api/setup-password", json={"password": PW2}).status_code == 200
    # новата парола + код влизат
    clock.t += 60
    c3 = client(ui)
    r = totp_login(c3, ADMIN, PW2, secret, clock, offset=0)
    assert r.status_code == 200


def test_one_time_key_redeem_is_gated_by_totp(ui, clock):
    c = bootstrap(ui)
    r = post(c, "/api/users", {"login": USER, "admin": False})
    key = r.json()["api_key"]
    cu = client(ui)
    assert odoo_login(cu, USER, pw=key).status_code == 200
    assert cu.post(f"{P}/api/setup-password", json={"password": PW}).status_code == 200
    secret, _ = enrol(cu, clock)
    # админът издава нов key ⇒ паролата пада, но факторът остава и пази redeem-а
    r = post(c, f"/api/users/{USER}/genkey")
    key2 = r.json()["api_key"]
    cu2 = client(ui)
    r = odoo_login(cu2, USER, pw=key2)
    assert r.status_code == 200 and r.json()["totp_required"] is True
    assert ui._load_user_auth(USER)["api_key_hash"]        # не е изгорен преди кода
    r = cu2.post(f"{P}/api/login/totp", json={"code": code_for(secret, clock, 1)})
    assert r.status_code == 200 and r.json()["next"] == f"{P}/setup"
    assert ui._load_user_auth(USER)["api_key_hash"] == ""


def test_totp_lockout_after_five_bad_codes(ui, clock):
    c = bootstrap(ui)
    secret, _ = enrol(c, clock)
    c2 = client(ui)
    mcp_login(c2, ADMIN, PW)
    for _ in range(5):
        assert c2.post(f"{P}/api/login/totp", json={"code": "000000"}).status_code == 401
    r = c2.post(f"{P}/api/login/totp", json={"code": code_for(secret, clock, 1)})
    assert r.status_code in (401, 429)                     # заключен по код и/или по опити
    assert c2.get(f"{P}/api/connections").status_code == 401


def test_policy_admins_forces_enrolment_only_for_admins(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("MCP_ADMIN_REQUIRE_TOTP", "admins")
    ui = _reload_ui(tmp_path, monkeypatch)
    c = bootstrap(ui)
    r = c.get(f"{P}/dashboard", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{P}/security?enroll=1"
    r = c.get(f"{P}/api/connections")
    assert r.status_code == 403 and r.json()["error"] == "totp_enrollment_required"
    assert c.get(f"{P}/security").status_code == 200
    secret, _ = enrol(c, clock)
    assert c.get(f"{P}/dashboard").status_code == 200
    assert c.get(f"{P}/api/connections").status_code == 200
    # политиката не позволява изключване
    r = post(c, "/api/totp/disable", {"password": PW, "code": code_for(secret, clock, 1)})
    assert r.status_code == 403 and ui._totp_enrolled(ADMIN)
    # обикновен потребител не е в обхвата
    cu = bootstrap(ui, login=USER)
    assert cu.get(f"{P}/dashboard").status_code == 200


def test_policy_all_covers_users_and_sessions_survive_reload(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("MCP_ADMIN_REQUIRE_TOTP", "all")
    ui = _reload_ui(tmp_path, monkeypatch)
    cu = bootstrap(ui, login=USER)
    assert cu.get(f"{P}/dashboard", follow_redirects=False).status_code == 302
    enrol(cu, clock)
    assert cu.get(f"{P}/dashboard").status_code == 200


def _reload_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_SECRET_TOKEN", "s" * 48)
    monkeypatch.setenv("MCP_KEY_PEPPER", "p" * 40)
    monkeypatch.setenv("MCP_BOOTSTRAP_ADMIN", ADMIN)
    import admin_ui
    importlib.reload(admin_ui)
    monkeypatch.setattr(admin_ui, "_validate_odoo",
                        lambda url, db, login, pw: 7 if pw == "odoo-pass" else None)

    async def _no_tarpit(_n):
        return None
    monkeypatch.setattr(admin_ui, "_tarpit_delay", _no_tarpit)
    monkeypatch.setattr(admin_ui, "_extension_routes", lambda: [])
    return admin_ui


def test_enrol_refuses_without_pepper(tmp_path, monkeypatch, clock):
    monkeypatch.delenv("MCP_ADMIN_REQUIRE_TOTP", raising=False)
    ui = _reload_ui(tmp_path, monkeypatch)
    monkeypatch.setenv("MCP_KEY_PEPPER", "short")
    c = bootstrap(ui)
    assert c.get(f"{P}/api/totp/status").json()["pepper_ready"] is False
    r = post(c, "/api/totp/enroll", {"password": PW})
    assert r.status_code == 400 and "MCP_KEY_PEPPER" in r.json()["error"]


def test_admin_can_reset_user_totp(ui, clock):
    c = bootstrap(ui)
    r = post(c, "/api/users", {"login": USER, "admin": False})
    key = r.json()["api_key"]
    cu = client(ui)
    odoo_login(cu, USER, pw=key)
    cu.post(f"{P}/api/setup-password", json={"password": PW})
    enrol(cu, clock)
    users = {u["login"]: u for u in c.get(f"{P}/api/users").json()["users"]}
    assert users[USER]["totp"] is True and users[ADMIN]["totp"] is False
    # не-админ не може
    r = cu.post(f"{P}/api/users/{ADMIN}/totp-reset", headers={"X-CSRF-Token": csrf(cu)})
    assert r.status_code == 403
    r = post(c, f"/api/users/{USER}/totp-reset")
    assert r.status_code == 200 and not ui._totp_enrolled(USER)
    assert post(c, f"/api/users/{USER}/totp-reset").status_code == 400
    r = mcp_login(client(ui), USER, PW)
    assert r.status_code == 200 and "totp_required" not in r.json()


def test_password_change_needs_code_and_revokes_other_sessions(ui, clock):
    c = bootstrap(ui)
    secret, _ = enrol(c, clock)
    other = client(ui)
    assert totp_login(other, ADMIN, PW, secret, clock, offset=1).status_code == 200
    assert other.get(f"{P}/api/connections").status_code == 200
    sess = c.get(f"{P}/api/sessions").json()["sessions"]
    assert len(sess) == 2 and sum(s["current"] for s in sess) == 1

    clock.t += 60
    r = post(c, "/api/password", {"current": "wrong", "new": PW2, "code": code_for(secret, clock)})
    assert r.status_code == 403
    r = post(c, "/api/password", {"current": PW, "new": "short", "code": code_for(secret, clock)})
    assert r.status_code == 400
    r = post(c, "/api/password", {"current": PW, "new": PW2, "code": "000000"})
    assert r.status_code == 401
    r = post(c, "/api/password", {"current": PW, "new": PW2, "code": code_for(secret, clock)})
    assert r.status_code == 200 and r.json()["sessions_revoked"] == 1
    assert other.get(f"{P}/api/connections").status_code == 401     # другата сесия е затворена
    assert c.get(f"{P}/api/connections").status_code == 200         # тази — не
    clock.t += 60
    assert mcp_login(client(ui), ADMIN, PW).status_code == 401       # старата парола е мъртва
    assert totp_login(client(ui), ADMIN, PW2, secret, clock, offset=0).status_code == 200


def test_revoke_other_sessions(ui):
    c = bootstrap(ui)
    other = client(ui)
    assert mcp_login(other, ADMIN, PW).status_code == 200
    r = post(c, "/api/sessions/revoke-others")
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert other.get(f"{P}/api/connections").status_code == 401
    assert c.get(f"{P}/api/connections").status_code == 200


def test_login_lockout_counts_per_account_not_only_per_ip(ui):
    bootstrap(ui)
    c = client(ui)
    for _ in range(5):
        assert mcp_login(c, ADMIN, "nope-nope-nope").status_code == 401
    assert mcp_login(c, ADMIN, PW).status_code == 429
    # друг профил от същия адрес също е заключен (IP), но и профилът е —
    # проверката по login е тази, която не се заобикаля с нов адрес.
    assert ui._lockout_remaining("10.0.0.99", ADMIN) > 0
    assert ui._lockout_remaining("10.0.0.99", USER) == 0


def test_audit_log_never_holds_secret_or_code(ui, clock):
    c = bootstrap(ui)
    secret, codes = enrol(c, clock)
    log = Path(ui.AUDIT_LOG).read_text()
    assert secret not in log
    for code in codes:
        assert code.replace("-", "") not in log
    events = [json.loads(line)["action"] for line in log.splitlines() if line.strip()]
    assert "totp_enroll_begin" in events and "totp_enroll" in events


def test_pages_render_for_admin(ui, clock):
    """Страниците са f-string-ове с JS вътре — една сбъркана скоба ги чупи
    едва при рендер. Затова всяка се отваря веднъж."""
    c = bootstrap(ui)
    for path, needle in (("/dashboard", "Втори фактор"), ("/connections", "function esc"),
                         ("/security", "enrollModal"), ("/users", "resetTotp")):
        r = c.get(f"{P}{path}")
        assert r.status_code == 200 and needle in r.text, path
        assert r.headers["content-security-policy"].startswith("default-src 'self'")
