"""Unit tests for OdooConnection auto-fallback jsonrpc→xmlrpc.

Context: Odoo 17+ /jsonrpc endpoint rejects api_key auth (HTTP 403) while
/xmlrpc/2/object accepts it. OdooConnection must detect this combo and
transparently use xmlrpc for data ops.

Run from project root:
    pytest tests/test_protocol_fallback.py -v
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# server.py is at project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP_ROOT = Path("/tmp/mcp-test-fallback")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MCP_STORAGE_DIR", str(_TMP_ROOT))
os.environ.setdefault("MCP_SSL_CERTS_DIR", str(_TMP_ROOT / "ssl_certs"))

import server  # noqa: E402


def _make_conn(**overrides):
    defaults = dict(
        alias="t",
        url="http://odoo.example",
        db="test",
        username="user",
        password="",
        api_key="",
        protocol="xmlrpc",
        verify_ssl=False,
    )
    defaults.update(overrides)
    return server.OdooConnection(**defaults)


# ── effective_protocol matrix ────────────────────────────────

class TestEffectiveProtocol:
    def test_jsonrpc_with_apikey_only_falls_back_to_xmlrpc(self):
        conn = _make_conn(protocol="jsonrpc", api_key="K")
        assert conn.protocol == "jsonrpc"
        assert conn.effective_protocol == "xmlrpc"

    def test_jsonrpc_with_password_stays_jsonrpc(self):
        conn = _make_conn(protocol="jsonrpc", password="P")
        assert conn.effective_protocol == "jsonrpc"

    def test_jsonrpc_with_both_password_and_apikey_stays_jsonrpc(self):
        # Password takes precedence — user explicitly set it.
        conn = _make_conn(protocol="jsonrpc", password="P", api_key="K")
        assert conn.effective_protocol == "jsonrpc"

    def test_xmlrpc_with_apikey_stays_xmlrpc(self):
        conn = _make_conn(protocol="xmlrpc", api_key="K")
        assert conn.effective_protocol == "xmlrpc"

    def test_xmlrpc_with_password_stays_xmlrpc(self):
        conn = _make_conn(protocol="xmlrpc", password="P")
        assert conn.effective_protocol == "xmlrpc"

    def test_effective_protocol_is_pure_no_side_effects(self):
        """Reading effective_protocol must not log (logging is in execute_kw)."""
        conn = _make_conn(protocol="jsonrpc", api_key="K")
        # Repeated reads must not flip _fallback_logged.
        _ = conn.effective_protocol
        _ = conn.effective_protocol
        assert conn._fallback_logged is False


# ── execute_kw routing ──────────────────────────────────────

class TestExecuteKwRouting:
    def test_jsonrpc_apikey_routes_to_xmlrpc_transport(self, monkeypatch):
        """jsonrpc+api_key must call xmlrpc ServerProxy, NOT _jsonrpc_call."""
        conn = _make_conn(protocol="jsonrpc", api_key="K")
        conn._uid = 7  # skip authenticate()
        conn._auth_token = "K"

        jsonrpc_mock = MagicMock(return_value="jsonrpc_result")
        monkeypatch.setattr(conn, "_jsonrpc_call", jsonrpc_mock)

        proxy_mock = MagicMock()
        proxy_mock.execute_kw.return_value = [1, 2, 3]
        sp_mock = MagicMock(return_value=proxy_mock)
        monkeypatch.setattr(server.xmlrpc.client, "ServerProxy", sp_mock)

        result = conn.execute_kw("res.partner", "search", [[]], {"limit": 5})

        assert result == [1, 2, 3]
        jsonrpc_mock.assert_not_called()
        proxy_mock.execute_kw.assert_called_once()
        # Verify it hit /xmlrpc/2/object
        called_url = sp_mock.call_args[0][0]
        assert called_url.endswith("/xmlrpc/2/object")

    def test_jsonrpc_password_routes_to_jsonrpc(self, monkeypatch):
        """jsonrpc+password must still use /jsonrpc (no fallback)."""
        conn = _make_conn(protocol="jsonrpc", password="P")
        conn._uid = 7
        conn._auth_token = "P"

        jsonrpc_mock = MagicMock(return_value="jsonrpc_result")
        monkeypatch.setattr(conn, "_jsonrpc_call", jsonrpc_mock)

        sp_mock = MagicMock()
        monkeypatch.setattr(server.xmlrpc.client, "ServerProxy", sp_mock)

        result = conn.execute_kw("res.partner", "search", [[]], {})

        assert result == "jsonrpc_result"
        jsonrpc_mock.assert_called_once_with("res.partner", "search", [[]], {})
        sp_mock.assert_not_called()

    def test_xmlrpc_apikey_routes_to_xmlrpc(self, monkeypatch):
        conn = _make_conn(protocol="xmlrpc", api_key="K")
        conn._uid = 7
        conn._auth_token = "K"

        jsonrpc_mock = MagicMock()
        monkeypatch.setattr(conn, "_jsonrpc_call", jsonrpc_mock)

        proxy_mock = MagicMock()
        proxy_mock.execute_kw.return_value = []
        sp_mock = MagicMock(return_value=proxy_mock)
        monkeypatch.setattr(server.xmlrpc.client, "ServerProxy", sp_mock)

        conn.execute_kw("res.partner", "search", [[]], {})

        jsonrpc_mock.assert_not_called()
        proxy_mock.execute_kw.assert_called_once()


# ── Warning log emitted exactly once ────────────────────────

class TestFallbackLogging:
    def test_fallback_logs_warning_once(self, monkeypatch, caplog):
        conn = _make_conn(protocol="jsonrpc", api_key="K")
        conn._uid = 7
        conn._auth_token = "K"

        proxy_mock = MagicMock()
        proxy_mock.execute_kw.return_value = []
        monkeypatch.setattr(
            server.xmlrpc.client, "ServerProxy",
            MagicMock(return_value=proxy_mock),
        )

        with caplog.at_level(logging.WARNING, logger=server.logger.name):
            conn.execute_kw("res.partner", "search", [[]], {})
            conn.execute_kw("res.partner", "search", [[]], {})
            conn.execute_kw("res.partner", "search", [[]], {})

        fallback_msgs = [
            r for r in caplog.records
            if "falling back to xmlrpc" in r.getMessage()
        ]
        assert len(fallback_msgs) == 1
        assert conn._fallback_logged is True

    def test_no_fallback_no_warning(self, monkeypatch, caplog):
        conn = _make_conn(protocol="xmlrpc", api_key="K")
        conn._uid = 7
        conn._auth_token = "K"

        proxy_mock = MagicMock()
        proxy_mock.execute_kw.return_value = []
        monkeypatch.setattr(
            server.xmlrpc.client, "ServerProxy",
            MagicMock(return_value=proxy_mock),
        )

        with caplog.at_level(logging.WARNING, logger=server.logger.name):
            conn.execute_kw("res.partner", "search", [[]], {})

        fallback_msgs = [
            r for r in caplog.records
            if "falling back to xmlrpc" in r.getMessage()
        ]
        assert fallback_msgs == []

    def test_jsonrpc_with_password_no_warning(self, monkeypatch, caplog):
        conn = _make_conn(protocol="jsonrpc", password="P")
        conn._uid = 7
        conn._auth_token = "P"

        monkeypatch.setattr(
            conn, "_jsonrpc_call", MagicMock(return_value=None),
        )

        with caplog.at_level(logging.WARNING, logger=server.logger.name):
            conn.execute_kw("res.partner", "search", [[]], {})

        fallback_msgs = [
            r for r in caplog.records
            if "falling back to xmlrpc" in r.getMessage()
        ]
        assert fallback_msgs == []


# ── to_dict exposes effective_protocol ──────────────────────

class TestToDictSurface:
    def test_to_dict_includes_effective_protocol(self):
        conn = _make_conn(protocol="jsonrpc", api_key="K")
        d = conn.to_dict()
        assert d["protocol"] == "jsonrpc"
        assert d["effective_protocol"] == "xmlrpc"
        assert d["has_api_key"] is True
        assert d["has_password"] is False

    def test_to_dict_effective_matches_protocol_when_no_fallback(self):
        conn = _make_conn(protocol="jsonrpc", password="P")
        d = conn.to_dict()
        assert d["protocol"] == "jsonrpc"
        assert d["effective_protocol"] == "jsonrpc"

    def test_to_dict_does_not_trigger_warning_log(self, caplog):
        """Introspection must be silent — only execute_kw logs."""
        conn = _make_conn(protocol="jsonrpc", api_key="K")
        with caplog.at_level(logging.WARNING, logger=server.logger.name):
            conn.to_dict()
            conn.to_dict()
        fallback_msgs = [
            r for r in caplog.records
            if "falling back to xmlrpc" in r.getMessage()
        ]
        assert fallback_msgs == []
        assert conn._fallback_logged is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
