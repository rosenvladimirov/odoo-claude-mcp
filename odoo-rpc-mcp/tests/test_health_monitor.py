"""B.6: health_monitor — classification + alert emission over a fake fleet."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import health_monitor as hm  # noqa: E402


class FakeFleet:
    def __init__(self, statuses):
        self._s = statuses

    def fleet_list(self):
        return {"stacks": [{"name": n, "client_id": n.split("-")[-1]}
                           for n in self._s], "portainer_reachable": True}

    def fleet_status(self, name):
        return self._s[name]


@pytest.fixture
def wired(monkeypatch):
    statuses = {
        "mcp-client-a": {"name": "mcp-client-a", "client_id": "a",
                         "health": {"healthy": True},
                         "mcp_probe": {"healthy": True, "tool_count": 120}},
        "mcp-client-b": {"name": "mcp-client-b", "client_id": "b",
                         "health": {"healthy": True},
                         "mcp_probe": {"healthy": False}},      # degraded
        "mcp-client-c": {"name": "mcp-client-c", "client_id": "c",
                         "health": {"healthy": False}},          # down
    }
    hm.wire(fleet_manager=FakeFleet(statuses))
    return hm


def test_scan_counts_and_alerts(wired):
    out = wired.health_scan()
    assert out["scanned"] == 3
    assert out["counts"]["healthy"] == 1
    assert out["counts"]["degraded"] == 1
    assert out["counts"]["down"] == 1
    assert out["alert_count"] == 2
    sev = {a["stack"]: a["severity"] for a in out["alerts"]}
    assert sev["mcp-client-c"] == "critical"
    assert sev["mcp-client-b"] == "warning"


def test_scan_excludes_healthy_by_default(wired):
    out = wired.health_scan()
    names = {r["name"] for r in out["results"]}
    assert "mcp-client-a" not in names           # healthy hidden
    assert {"mcp-client-b", "mcp-client-c"} <= names


def test_scan_include_healthy(wired):
    out = wired.health_scan(include_healthy=True)
    names = {r["name"] for r in out["results"]}
    assert "mcp-client-a" in names


def test_stack_health_classification(wired):
    assert wired.stack_health("mcp-client-a")["state"] == "healthy"
    assert wired.stack_health("mcp-client-b")["state"] == "degraded"
    assert wired.stack_health("mcp-client-c")["state"] == "down"


def test_not_wired():
    hm.wire(fleet_manager=None)
    assert "error" in hm.health_scan()
