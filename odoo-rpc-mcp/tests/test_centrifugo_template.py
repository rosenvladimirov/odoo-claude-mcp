"""Regression: per-stack Centrifugo must survive template renders.

The centrifugo sidecar is configured ENTIRELY via env (no host config.json),
so the whole contract lives in client_stack.template.yml + render_compose().
These tests fail loudly if a future template edit drops the service, the
secret substitution, or the telegram/discuss namespaces.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import provisioning_engine as pe  # noqa: E402

yaml = pytest.importorskip("yaml")


def _render():
    hmac = pe.generate_centrifugo_secret()
    api_key = pe.generate_centrifugo_secret()
    compose = pe.render_compose(
        "bg999test", "sectok", "admintok", anthropic_key="",
        centrifugo_hmac=hmac, centrifugo_api_key=api_key,
    )
    return compose, hmac, api_key


def test_secret_is_64_hex():
    s = pe.generate_centrifugo_secret()
    assert len(s) == 64
    int(s, 16)  # raises if not hex


def test_no_unsubstituted_placeholders():
    compose, _, _ = _render()
    assert "{{" not in compose and "}}" not in compose


def test_centrifugo_service_present_and_valid_yaml():
    compose, _, _ = _render()
    doc = yaml.safe_load(compose)
    assert "centrifugo-bg999test" in doc["services"]


def test_namespaces_are_telegram_and_discuss():
    compose, _, _ = _render()
    doc = yaml.safe_load(compose)
    env = doc["services"]["centrifugo-bg999test"]["environment"]
    ns_line = next(e for e in env if e.startswith("CENTRIFUGO_CHANNEL_NAMESPACES="))
    namespaces = json.loads(ns_line.split("=", 1)[1])
    assert [n["name"] for n in namespaces] == ["telegram", "discuss"]


def test_secrets_injected_into_both_services():
    compose, hmac, api_key = _render()
    doc = yaml.safe_load(compose)
    cf_env = doc["services"]["centrifugo-bg999test"]["environment"]
    assert any(hmac in e for e in cf_env)
    assert any(api_key in e for e in cf_env)
    # odoo-rpc-mcp publishes to the hub — it must carry the same wiring.
    mcp_env = doc["services"]["odoo-rpc-mcp-bg999test"]["environment"]
    assert any(e == f"CENTRIFUGO_API_URL=http://centrifugo-bg999test:8000/api/publish"
               for e in mcp_env)
    assert any(api_key in e for e in mcp_env)
    assert any(hmac in e for e in mcp_env)
