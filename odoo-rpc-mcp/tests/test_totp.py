"""3.3.0: TOTP two-factor for name-identify.

Offline logic tests — redirect DATA_DIR + set MCP_KEY_PEPPER into tmp, reload
`server`, then exercise the pure TOTP helpers and the enrol/verify/disable path
(without touching the session store). Mirrors tests/test_secrets_registry.py.
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_KEY_PEPPER", "p" * 40)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    # secrets_registry reads the pepper live, but reload to be safe.
    import secrets_registry
    importlib.reload(secrets_registry)
    importlib.reload(server)
    return server


def _wrong_code(server, secret: str) -> str:
    """A 6-digit code guaranteed NOT to match the current ±1 window."""
    now_step = int(time.time()) // server._TOTP_STEP
    valid = {server._totp_code(secret, now_step + w) for w in (-1, 0, 1)}
    for cand in ("000000", "111111", "222222", "999999", "123456"):
        if cand not in valid:
            return cand
    raise AssertionError("could not pick a wrong code")


# ── pure TOTP maths ───────────────────────────────────────────

def test_code_roundtrip_and_window(srv):
    secret = srv._totp_secret_new()
    step = int(time.time()) // srv._TOTP_STEP
    code = srv._totp_code(secret, step)
    assert len(code) == srv._TOTP_DIGITS and code.isdigit()
    # exact step verifies
    ok, matched = srv._totp_verify_secret(secret, code, at=step * srv._TOTP_STEP)
    assert ok and matched == step
    # ±1 step within window
    prev = srv._totp_code(secret, step - 1)
    ok, _ = srv._totp_verify_secret(secret, prev, at=step * srv._TOTP_STEP)
    assert ok
    # 2 steps away → outside window → rejected
    far = srv._totp_code(secret, step - 2)
    ok, _ = srv._totp_verify_secret(secret, far, at=step * srv._TOTP_STEP)
    assert not ok


def test_malformed_code_rejected(srv):
    secret = srv._totp_secret_new()
    assert srv._totp_verify_secret(secret, "12345")[0] is False   # too short
    assert srv._totp_verify_secret(secret, "abcdef")[0] is False  # non-digit
    assert srv._totp_verify_secret(secret, "")[0] is False


# ── enrol / verify / replay / disable ─────────────────────────

def test_enroll_status_verify_replay_disable(srv):
    p = "rosen"
    assert srv._totp_enrolled(p) is False
    out = srv._totp_enroll(p)
    assert out["status"] == "enrolled"
    assert out["otpauth_uri"].startswith("otpauth://totp/")
    secret = out["secret"]
    assert srv._totp_enrolled(p) is True
    # status tool
    st = srv._totp_tool_handle("identify_totp_status", {}, p)
    assert st["enrolled"] is True and st["pepper_ready"] is True
    # secret is stored ENCRYPTED, never in plaintext on disk
    raw = Path(srv._totp_path(p)).read_text(encoding="utf-8")
    assert secret not in raw and "secret_enc" in raw
    # verify current code
    step = int(time.time()) // srv._TOTP_STEP
    code = srv._totp_code(secret, step)
    res = srv._totp_check(p, code)
    assert res["ok"] is True
    # replay of the SAME step is rejected
    res2 = srv._totp_check(p, code)
    assert res2["ok"] is False and res2["reason"] == "replay"
    # disable removes the secret
    dis = srv._totp_tool_handle("identify_totp_disable", {}, p)
    assert dis["status"] == "disabled"
    assert srv._totp_enrolled(p) is False


def test_enroll_returns_qr(srv):
    out = srv._totp_enroll("qruser")
    assert out["status"] == "enrolled"
    # QR fields present (segno pinned in requirements)
    assert out["qr_svg"].startswith("data:image/svg+xml")
    assert "█" in out["qr_ascii"] or "▀" in out["qr_ascii"]
    # ASCII QR is multi-line and reasonably sized
    assert out["qr_ascii"].count("\n") > 10
    # QR encodes the otpauth URI (the secret is inside it, shown once anyway)
    assert out["otpauth_uri"].startswith("otpauth://totp/")


def test_reenroll_requires_force(srv):
    p = "ivan"
    srv._totp_enroll(p)
    again = srv._totp_enroll(p)
    assert again["status"] == "already_enrolled"
    forced = srv._totp_enroll(p, force=True)
    assert forced["status"] == "enrolled"


def test_rate_limit_lockout(srv):
    p = "petar"
    out = srv._totp_enroll(p)
    wrong = _wrong_code(srv, out["secret"])
    reasons = [srv._totp_check(p, wrong)["reason"] for _ in range(srv._TOTP_MAX_FAILS)]
    assert reasons[:-1] == ["invalid_code"] * (srv._TOTP_MAX_FAILS - 1)
    # next attempt is locked out
    locked = srv._totp_check(p, wrong)
    assert locked["ok"] is False and locked["reason"] == "locked"
    assert locked["retry_after"] > 0


def test_enroll_tool_requires_identity(srv):
    res = srv._totp_tool_handle("identify_totp_enroll", {}, None)
    assert res["status"] == "error" and res["error"] == "no_identity"


# ── fail-closed without pepper ────────────────────────────────

def test_fail_closed_without_pepper(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_KEY_PEPPER", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    import secrets_registry
    importlib.reload(secrets_registry)
    importlib.reload(server)
    out = server._totp_enroll("rosen")
    assert out["status"] == "error" and out["error"] == "weak_or_missing_pepper"
    assert server._totp_pepper_ok() is False
