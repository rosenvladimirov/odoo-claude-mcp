"""Regression tests for the 2026-06-10 security audit fixes.

Covers the pure/unit-testable surface of the P0/P1/P2 fixes:
  - _sanitize_name rejects path-traversal components
  - _safe_save_path confines writes under the principal's downloads dir
  - _ssrf_target_allowed blocks private/loopback targets
  - _hmac_eq constant-time compare semantics
HTTP-layer fixes (OAuth, IDOR, AI-bypass) are exercised by the live-server
smoke run; these are the deterministic unit checks.
"""
import os
import sys
import tempfile
import importlib
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def srv():
    # Import server.py with throwaway data dirs so module import is side-effect free.
    tmp = tempfile.mkdtemp(prefix="sectest-")
    os.environ.setdefault("DATA_DIR", tmp)
    os.environ.setdefault("MCP_SESSIONS_DB", os.path.join(tmp, "s.db"))
    os.environ.setdefault("SESSIONS_DB", os.path.join(tmp, "w.db"))
    os.environ.setdefault("CONNECTIONS_FILE", os.path.join(tmp, "c.json"))
    import server
    importlib.reload(server)
    return server


# ── _sanitize_name traversal guard ──
@pytest.mark.parametrize("evil", ["..", ".", "...", "./.", "../", "..\\", "  ..  "])
def test_sanitize_name_rejects_traversal(srv, evil):
    out = srv._sanitize_name(evil)
    assert out not in (".", "..", ""), f"{evil!r} -> {out!r}"
    assert ".." not in out
    # must be a plain single component
    assert "/" not in out and "\\" not in out


def test_sanitize_name_keeps_normal(srv):
    assert srv._sanitize_name("Rosen") == "rosen"
    assert srv._sanitize_name("lyubomir.topalov@teolino.eu") == "lyubomir.topalov_teolino.eu"


# ── _safe_save_path confinement ──
# Portable across v2 (per-principal downloads dir) and v3 (MCP_DOWNLOAD_ROOT):
# both confine writes to an allowed root. For an out-of-root absolute path the
# two differ — v2 treats it as a relative leaf (confined), v3 rejects it
# (ValueError). Either behavior is safe; the invariant is "never escapes root".
def test_safe_save_path_blocks_absolute_and_traversal(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "_require_principal", lambda: "tester")
    # v3 confines under MCP_DOWNLOAD_ROOT (default /data/downloads, unwritable on
    # the test host) — point it at tmp. v2 ignores this env (per-principal dir).
    monkeypatch.setenv("MCP_DOWNLOAD_ROOT", str(tmp_path / "downloads"))

    # benign relative leaf must resolve to a real path (and create dirs ok)
    p = srv._safe_save_path("report.pdf")
    assert os.path.isabs(p)

    # an out-of-root absolute path is either rejected OR confined — never honored
    try:
        p2 = srv._safe_save_path("/etc/cron.d/x")
        assert not p2.startswith("/etc/"), f"escaped root: {p2}"
    except ValueError:
        pass  # v3: rejected outright — also safe

    # parent-traversal escapes are always rejected
    for evil in ("../../../etc/passwd", "../../outside", "a/../../../../etc/x"):
        with pytest.raises(ValueError):
            srv._safe_save_path(evil)


# ── SSRF target guard ──
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8069", "http://localhost/", "http://10.1.2.3",
    "http://192.168.0.5:8069", "http://169.254.169.254/latest/meta-data",
    "http://qdrant:6333",  # docker service name → resolves internal or NXDOMAIN
])
def test_ssrf_blocks_private(srv, url):
    # qdrant won't resolve on the test host → guard returns True (whitelist is
    # the real gate); the explicit private IPs MUST be blocked.
    host = url.split("//", 1)[1].split("/")[0].split(":")[0]
    if host == "qdrant":
        pytest.skip("docker-only hostname; not resolvable on test host")
    assert srv._ssrf_target_allowed(url) is False


@pytest.mark.parametrize("url", [
    "https://odoo-shell.space", "https://mcp.odoo-shell.space/",
])
def test_ssrf_allows_public(srv, url):
    assert srv._ssrf_target_allowed(url) is True


# ── constant-time compare ──
def test_hmac_eq(srv):
    assert srv._hmac_eq("abc", "abc") is True
    assert srv._hmac_eq("abc", "abd") is False
    assert srv._hmac_eq("", "abc") is False
    assert srv._hmac_eq("abc", "") is False
