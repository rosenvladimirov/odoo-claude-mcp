"""B.1: fleet_manager inventory + canary upgrade/rollback (Portainer mocked)."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fleet_manager as fm  # noqa: E402

COMPOSE_V9 = """services:
  odoo-rpc-mcp:
    image: vladimirovrosen/odoo-rpc-mcp:3.0.0-alpha.9
    ports: ["8094:8094"]
"""


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(fm, "FLEET_OPS_FILE", tmp_path / "fleet_ops.jsonl")
    monkeypatch.setattr(fm, "DRY_RUN", True)
    monkeypatch.setattr(fm, "DRY_RUN_PREVIEW_DIR", tmp_path / "preview")
    services = {
        "client-115572378": {"url": "https://mcp-115572378.mcpworks.net"},
    }
    fm.wire(get_proxy_services=lambda: services, discover_one=lambda n: [object()])
    # Portainer: one matching stack.
    monkeypatch.setattr(fm, "_portainer_list_stacks", lambda: {
        "ok": True, "stacks": [{"Id": 42, "Name": "mcp-client-115572378",
                                "Status": 1}]})
    monkeypatch.setattr(fm, "_portainer_get_stack_file",
                        lambda sid: {"ok": True, "compose": COMPOSE_V9})
    return fm


def test_fleet_list(wired):
    out = wired.fleet_list()
    assert out["count"] == 1
    row = out["stacks"][0]
    assert row["client_id"] == "115572378"
    assert row["portainer_stack_id"] == 42


def test_tag_helpers(wired):
    assert wired._current_tag(COMPOSE_V9) == "3.0.0-alpha.9"
    new, n = wired._swap_tag(COMPOSE_V9, "3.0.0-alpha.10")
    assert n == 1 and "alpha.10" in new and "alpha.9" not in new


def test_upgrade_dry_run_no_mutation(wired, monkeypatch):
    called = {"put": 0}
    monkeypatch.setattr(wired, "portainer_update_stack",
                        lambda *a, **k: called.__setitem__("put", called["put"] + 1) or {"ok": True})
    out = wired.fleet_upgrade("115572378", "3.0.0-alpha.10")
    assert out["dry_run"] is True
    assert out["from_tag"] == "3.0.0-alpha.9"
    assert out["to_tag"] == "3.0.0-alpha.10"
    assert called["put"] == 0          # NO Portainer mutation in dry-run
    assert Path(out["preview"]).is_file()


def test_upgrade_real_healthy(wired, monkeypatch):
    monkeypatch.setattr(wired, "portainer_update_stack",
                        lambda *a, **k: {"ok": True, "stack_id": 42})
    monkeypatch.setattr(wired.pe, "wait_for_health",
                        lambda *a, **k: {"healthy": True})
    out = wired.fleet_upgrade("115572378", "3.0.0-alpha.10", dry_run=False)
    assert out["ok"] is True
    assert out["to_tag"] == "3.0.0-alpha.10"


def test_upgrade_unhealthy_auto_rollback(wired, monkeypatch):
    calls = []
    def fake_update(sid, compose, **k):
        calls.append(wired._current_tag(compose))
        return {"ok": True, "stack_id": sid}
    monkeypatch.setattr(wired, "portainer_update_stack", fake_update)
    # first health (new tag) fails, rollback health ok
    seq = iter([{"healthy": False}, {"healthy": True}])
    monkeypatch.setattr(wired.pe, "wait_for_health", lambda *a, **k: next(seq))
    out = wired.fleet_upgrade("115572378", "3.0.0-alpha.10", dry_run=False)
    assert out["error"] == "upgrade_unhealthy_rolled_back"
    # deployed alpha.10 then rolled back to alpha.9
    assert calls == ["3.0.0-alpha.10", "3.0.0-alpha.9"]


def test_upgrade_no_change(wired):
    out = wired.fleet_upgrade("115572378", "3.0.0-alpha.9", dry_run=False)
    assert out.get("no_change") is True


def test_upgrade_unknown_stack(wired):
    out = wired.fleet_upgrade("nope", "x", dry_run=False)
    assert out["error"] == "unknown_stack"


def test_rollback_uses_snapshot(wired, monkeypatch):
    # dry-run upgrade records a snapshot, then rollback reads it
    wired.fleet_upgrade("115572378", "3.0.0-alpha.10")  # dry-run, appends op w/ snapshot
    monkeypatch.setattr(wired, "portainer_update_stack",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(wired.pe, "wait_for_health", lambda *a, **k: {"healthy": True})
    out = wired.fleet_rollback("115572378", dry_run=False)
    assert out["ok"] is True
    assert out["to_tag"] == "3.0.0-alpha.9"
