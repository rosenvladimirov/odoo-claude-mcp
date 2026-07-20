"""B.2: module_deploy — dry-run plan, ephemeral upgrade, runtime gate, rollback.

SSH transport and Odoo RPC are mocked; no real host is touched.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import module_deploy as md  # noqa: E402


class FakeConn:
    url = "https://erp.example.com"

    def __init__(self, state="installed", version="18.0.1.0.0", errors=0):
        self._state = state
        self._version = version
        self._errors = errors

    def execute_kw(self, model, method, args, kwargs=None):
        if model == "ir.module.module" and method == "search_read":
            return [{"state": self._state, "installed_version": self._version,
                     "latest_version": self._version}]
        if model == "ir.logging" and method == "search_count":
            return self._errors
        return []


@pytest.fixture
def env(tmp_path, monkeypatch):
    # connections.json with a deploy-capable target
    conns = {
        "demo": {
            "url": "https://erp.example.com", "db": "demo",
            "ssh": {"host": "h", "user": "u", "port": 22},
            "deploy": {"addons_path": "/opt/addons", "container": "odoo",
                       "db_container": "db", "db": "demo",
                       "ephemeral_image": "odoo:18"},
        }
    }
    cfile = tmp_path / "connections.json"
    cfile.write_text(json.dumps(conns))
    monkeypatch.setattr(md, "_CONN_CANDIDATES", [cfile])
    monkeypatch.setattr(md, "DEPLOY_OPS_FILE", tmp_path / "ops.jsonl")
    monkeypatch.setattr(md, "DRY_RUN", True)
    monkeypatch.setattr(md, "REPOS_DIR", str(tmp_path / "repos"))
    # local module
    mod = tmp_path / "repos" / "l10n_bg_demo"
    mod.mkdir(parents=True)
    (mod / "__manifest__.py").write_text("{'name':'demo','version':'18.0.1.0.0'}")
    # never hit the network in the runtime gate
    monkeypatch.setattr(md.pe, "wait_for_health", lambda *a, **k: {"healthy": True})
    # wired transport/rpc
    md.wire(ssh_execute=lambda host, user, command, **k: {
                "status": "ok", "exit_code": 0, "stdout": "", "stderr": ""},
            ensure_ssh_master=lambda *a, **k: "/tmp/ctl",
            get_conn=lambda alias: FakeConn())
    return md, str(mod)


def test_deploy_dry_run_plan(env):
    md_, src = env
    out = md_.deploy_module("demo", "l10n_bg_demo", source=src)
    assert out["dry_run"] is True
    assert "rsync" in out["plan"]["rsync"]["rsync"]
    assert out["plan"]["upgrade"]["steps"]["upgrade"].count("-u") == 1
    assert "--stop-after-init" in out["plan"]["upgrade"]["steps"]["upgrade"]


def test_deploy_unknown_target(env):
    md_, _ = env
    out = md_.deploy_module("nope", "x")
    assert out["error"] == "unknown_target_or_no_deploy_config"


def test_deploy_local_module_missing(env):
    md_, _ = env
    out = md_.deploy_module("demo", "does_not_exist")
    assert out["error"] == "local_module_not_found"


def test_i18n_overwrite_flag(env):
    md_, src = env
    out = md_.deploy_module("demo", "l10n_bg_demo", source=src, i18n_overwrite=True)
    assert "--i18n-overwrite" in out["plan"]["upgrade"]["steps"]["upgrade"]


def test_real_deploy_healthy_gate(env, monkeypatch):
    md_, src = env
    monkeypatch.setattr(md_, "DRY_RUN", False)
    # rsync subprocess + health mocked
    monkeypatch.setattr(md_, "_rsync_module",
                        lambda dep, m, s, d: {"ok": True, "backup": "/b.tar.gz"})
    monkeypatch.setattr(md_.pe, "wait_for_health",
                        lambda *a, **k: {"healthy": True})
    out = md_.deploy_module("demo", "l10n_bg_demo", source=src, dry_run=False)
    assert out["ok"] is True
    assert out["gate"]["state_ok"] is True
    assert out["gate"]["passed"] is True


def test_real_deploy_upgrade_traceback_fails(env, monkeypatch):
    md_, src = env
    monkeypatch.setattr(md_, "DRY_RUN", False)
    monkeypatch.setattr(md_, "_rsync_module",
                        lambda dep, m, s, d: {"ok": True, "backup": "/b.tar.gz"})

    def ssh(host, user, command, **k):
        out = {"status": "ok", "exit_code": 0, "stdout": "", "stderr": ""}
        if "docker run" in command:
            out["stdout"] = "Loading... Traceback (most recent call last): boom"
        return out
    md_.wire(ssh_execute=ssh, ensure_ssh_master=lambda *a, **k: "/tmp/ctl",
             get_conn=lambda alias: FakeConn())
    out = md_.deploy_module("demo", "l10n_bg_demo", source=src, dry_run=False)
    assert out["error"] == "upgrade_failed"
    assert out["backup"] == "/b.tar.gz"


def test_gate_detects_log_errors(env):
    md_, src = env
    md_.wire(ssh_execute=lambda *a, **k: {"status": "ok", "exit_code": 0,
             "stdout": "", "stderr": ""},
             ensure_ssh_master=lambda *a, **k: "/tmp/ctl",
             get_conn=lambda alias: FakeConn(errors=3))
    st = md_.deploy_status("demo", "l10n_bg_demo")
    assert st["gate"]["log_errors"] == 3
    assert st["gate"]["log_ok"] is False
    assert st["gate"]["passed"] is False


def test_rollback_no_backup(env):
    md_, _ = env
    out = md_.deploy_rollback("demo", "l10n_bg_demo")
    assert out["error"] == "no_backup_to_restore"


def test_history(env):
    md_, src = env
    md_.deploy_module("demo", "l10n_bg_demo", source=src)  # dry-run op recorded
    h = md_.deploy_history("demo")
    assert h["count"] >= 1
    assert h["ops"][0]["module"] == "l10n_bg_demo"
