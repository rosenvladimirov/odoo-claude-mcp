"""A/B.8: session_handoff — two-phase consent, principal-bound accept,
transfer-vs-share, TTL, audit. No credential transfer.
"""
from __future__ import annotations
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sh(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_AUDIT_FILE", str(tmp_path / "audit.log"))
    import session_handoff
    importlib.reload(session_handoff)
    return session_handoff


def test_offer_accept_transfer_revokes_offerer(sh):
    tenants = {}
    revoked = []
    sh.wire(set_tenant=lambda sk, t: tenants.__setitem__(sk, t),
            revoke_session=lambda sk: revoked.append(sk) or {"ok": True})
    off = sh.offer("mcp:A", "rosen", "lyubomir",
                   include=["tenant", "note"],
                   payload={"tenant": "clientX", "note": "поеми Терарос"},
                   mode="transfer")
    hid = off["handoff_id"]
    assert off["status"] == "pending"
    out = sh.accept(hid, "mcp:B", "lyubomir")
    assert out["ok"] is True
    assert out["materialized"]["tenant"] == {"active": "clientX"}
    assert out["materialized"]["note"] == "поеми Терарос"
    assert tenants["mcp:B"] == "clientX"      # materialized into ACCEPTER's session
    assert revoked == ["mcp:A"]               # offerer revoked (transfer)
    assert out["offering_session_revoked"] is True


def test_share_mode_keeps_offerer(sh):
    revoked = []
    sh.wire(revoke_session=lambda sk: revoked.append(sk))
    off = sh.offer("mcp:A", "rosen", "lyubomir", include=["note"],
                   mode="share", note="hi")
    out = sh.accept(off["handoff_id"], "mcp:B", "lyubomir")
    assert out["mode"] == "share"
    assert revoked == []                       # offerer NOT revoked
    assert out["offering_session_revoked"] is False


def test_accept_wrong_principal_denied(sh):
    off = sh.offer("mcp:A", "rosen", "lyubomir", include=["note"])
    out = sh.accept(off["handoff_id"], "mcp:C", "someone_else")
    assert out["error"] == "not_addressed_to_you"
    # still pending for the right principal
    assert sh.status(off["handoff_id"], "lyubomir")["status"] == "pending"


def test_cannot_handoff_to_self(sh):
    out = sh.offer("mcp:A", "rosen", "rosen")
    assert out["error"] == "cannot_handoff_to_self"


def test_offer_requires_identity(sh):
    assert sh.offer(None, None, "x")["error"] == "no_identity"


def test_invalid_mode(sh):
    assert sh.offer("mcp:A", "rosen", "x", mode="steal")["error"] == "invalid_mode"


def test_status_only_parties(sh):
    off = sh.offer("mcp:A", "rosen", "lyubomir", include=["note"])
    assert sh.status(off["handoff_id"], "rosen")["status"] == "pending"
    assert sh.status(off["handoff_id"], "lyubomir")["status"] == "pending"
    assert sh.status(off["handoff_id"], "stranger")["error"] == "not_a_party"


def test_cancel_only_offerer(sh):
    off = sh.offer("mcp:A", "rosen", "lyubomir")
    assert sh.cancel(off["handoff_id"], "lyubomir")["error"] == "only_offerer_can_cancel"
    assert sh.cancel(off["handoff_id"], "rosen")["ok"] is True
    # gone now
    assert sh.accept(off["handoff_id"], "mcp:B", "lyubomir")["error"] \
        == "unknown_or_expired_handoff"


def test_ttl_expiry(sh):
    off = sh.offer("mcp:A", "rosen", "lyubomir", ttl=1)
    time.sleep(1.1)
    assert sh.accept(off["handoff_id"], "mcp:B", "lyubomir")["error"] \
        == "unknown_or_expired_handoff"


def test_double_accept_blocked(sh):
    sh.wire(revoke_session=lambda sk: {"ok": True})
    off = sh.offer("mcp:A", "rosen", "lyubomir", include=["note"])
    sh.accept(off["handoff_id"], "mcp:B", "lyubomir")
    out2 = sh.accept(off["handoff_id"], "mcp:B", "lyubomir")
    assert out2["error"] == "handoff_not_pending"


def test_audit_written(sh):
    off = sh.offer("mcp:A", "rosen", "lyubomir", include=["note"])
    sh.accept(off["handoff_id"], "mcp:B", "lyubomir")
    lines = [json.loads(l) for l in
             Path(sh.HANDOFF_AUDIT).read_text().splitlines()]
    actions = {l["action"] for l in lines}
    assert {"OFFERED", "ACCEPTED"} <= actions


def test_pending_list_for_principal(sh):
    sh.offer("mcp:A", "rosen", "lyubomir", include=["note"])
    sh.offer("mcp:A", "rosen", "boyan", include=["note"])
    lst = sh.status(None, "rosen")
    assert lst["count"] == 2
    lst_l = sh.status(None, "lyubomir")
    assert lst_l["count"] == 1
