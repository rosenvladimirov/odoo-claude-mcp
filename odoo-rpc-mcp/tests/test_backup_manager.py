"""B.5: backup_manager — dry-run plan, backup, staging-guarded restore."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backup_manager as bm  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    conns = {
        "demo": {"db": "demo", "ssh": {"host": "h", "user": "u"},
                 "deploy": {"db_container": "db", "db": "demo",
                            "filestore_path": "/fs"}},
        "prod": {"db": "prod", "ssh": {"host": "h", "user": "u"},
                 "deploy": {"db_container": "db", "db": "prod",
                            "allow_restore": True}},
    }
    cfile = tmp_path / "connections.json"
    cfile.write_text(json.dumps(conns))
    monkeypatch.setattr(bm, "_CONN_CANDIDATES", [cfile])
    monkeypatch.setattr(bm, "BACKUP_OPS_FILE", tmp_path / "ops.jsonl")
    monkeypatch.setattr(bm, "DRY_RUN", True)
    bm.wire(ssh_execute=lambda host, user, command, **k: {
        "status": "ok", "exit_code": 0, "stdout": "", "stderr": ""})
    return bm


def test_backup_dry_run_plan(env):
    out = env.tenant_backup("demo")
    assert out["dry_run"] is True
    assert "pg_dump" in out["plan"]["dump"]
    assert "tar czf" in out["plan"]["filestore"]


def test_backup_unknown_target(env):
    assert env.tenant_backup("nope")["error"] == "unknown_target_or_no_config"


def test_backup_real_records_artifact(env, monkeypatch):
    monkeypatch.setattr(env, "DRY_RUN", False)
    out = env.tenant_backup("demo", dry_run=False)
    assert out["ok"] is True
    assert out["artifact"]
    lst = env.backup_list("demo")
    assert lst["count"] == 1


def test_restore_defaults_to_staging(env):
    out = env.tenant_restore("demo", artifact="/b/demo-1", to_staging=True)
    assert out["dry_run"] is True
    assert out["plan"]["restore_db"] == "demo_staging"


def test_restore_prod_blocked_without_flag(env):
    out = env.tenant_restore("demo", artifact="/b/demo-1", to_staging=False)
    assert out["error"] == "prod_restore_blocked"


def test_restore_prod_allowed_with_flag(env):
    out = env.tenant_restore("prod", artifact="/b/prod-1", to_staging=False)
    assert out["dry_run"] is True
    assert out["plan"]["restore_db"] == "prod"


def test_restore_uses_last_backup_when_no_artifact(env, monkeypatch):
    monkeypatch.setattr(env, "DRY_RUN", False)
    env.tenant_backup("demo", dry_run=False)        # records artifact
    monkeypatch.setattr(env, "DRY_RUN", True)
    out = env.tenant_restore("demo")                # no artifact → use last
    assert out["dry_run"] is True
    assert "demo-" in out["plan"]["restore"]
