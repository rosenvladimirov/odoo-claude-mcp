"""B.7: tenant_migrate — assess pipeline + manual cutover plan (no auto-apply)."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tenant_migrate as tm  # noqa: E402


class FakeBackup:
    def tenant_backup(self, target, dry_run=None):
        return {"ok": True, "artifact": f"/b/{target}-1", "dry_run": dry_run}

    def tenant_restore(self, target, artifact="", to_staging=True, dry_run=None):
        return {"ok": True, "restore_db": f"{target}_staging", "dry_run": dry_run}


class FakeHealth:
    def stack_health(self, name):
        return {"name": name, "state": "healthy"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    conns = {
        "prod18": {"deploy": {"odoo_version": "18.0", "db": "p18",
                              "db_container": "db"}},
        "stage19": {"deploy": {"odoo_version": "19.0", "db": "p19",
                               "db_container": "db"}},
    }
    cfile = tmp_path / "connections.json"
    cfile.write_text(json.dumps(conns))
    monkeypatch.setattr(tm, "_CONN_CANDIDATES", [cfile])
    monkeypatch.setattr(tm, "MIGRATE_OPS_FILE", tmp_path / "ops.jsonl")
    monkeypatch.setattr(tm, "DRY_RUN", True)
    tm.wire(backup_manager=FakeBackup(), health_monitor=FakeHealth())
    return tm


def test_assess_dry_run_emits_plan(env):
    out = env.migrate_assess("prod18", "stage19", to_version="19.0")
    assert out["ok"] is True
    assert out["from_version"] == "18.0"
    assert out["to_version"] == "19.0"
    assert len(out["cutover_plan"]) == 8
    # plan is manual: there is no auto-apply verb in the API
    assert not any(s["action"] == "apply" for s in out["cutover_plan"])
    assert out["steps"]["staging_health"]["state"] == "healthy"
    assert out["risk_notes"]


def test_assess_unknown_source(env):
    assert env.migrate_assess("nope")["error"] == "unknown_source"


def test_assess_records_history(env):
    env.migrate_assess("prod18", "stage19")
    h = env.migrate_history("prod18")
    assert h["count"] == 1
    assert h["ops"][0]["source"] == "prod18"


def test_no_apply_verb_exists(env):
    # safety: migration must not expose an auto-cutover tool
    assert env.ADMIN_TOOL_NAMES == {"migrate_assess", "migrate_history"}


def test_backup_failure_aborts(env, monkeypatch):
    class FailBackup:
        def tenant_backup(self, target, dry_run=None):
            return {"error": "boom"}
    tm.wire(backup_manager=FailBackup(), health_monitor=FakeHealth())
    out = env.migrate_assess("prod18", "stage19")
    assert out["error"] == "backup_failed"
