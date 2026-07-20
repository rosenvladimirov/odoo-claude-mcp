"""tg_listen_helper — runbook generator for the local Claude (no infra touch)."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tg_listen_helper as h  # noqa: E402


def test_activate_full_runbook():
    r = h.activate("teolino", chat_id="-5191192785", title="Теолино dev")
    assert r["session"] == "teolino"
    assert r["chat_id"] == "-5191192785"
    assert len(r["runbook"]) == 7
    # has the login + code step (the Telegram-generated key)
    step4 = next(s for s in r["runbook"] if s["n"] == 4)
    joined = " ".join(step4["run"])
    assert "session login teolino" in joined
    assert "session code teolino <КОД>" in joined
    # differentiated: monitor tails this session's own inbox
    step6 = next(s for s in r["runbook"] if s["n"] == 6)
    assert "inbox/teolino.ndjson" in step6["run"][0]


def test_activate_requires_session():
    assert "error" in h.activate("")


def test_activate_fresh_login_warns_against_copy():
    r = h.activate("new", chat_id="-100")
    cmd = next(s["run"][0] for s in r["runbook"] if s["n"] == 2)
    # the actual command uses fresh --api-id; the comment WARNS against --from-master
    command_part = cmd.split("#", 1)[0]
    assert "--api-id" in command_part and "--from-master" not in command_part
    assert "NOT --from-master" in cmd      # warning present in the comment


def test_activate_from_master_when_disabled():
    r = h.activate("x", fresh_login=False)
    step2 = next(s for s in r["runbook"] if s["n"] == 2)
    assert "--from-master" in step2["run"][0]


def test_activate_no_chat_gives_placeholder():
    r = h.activate("x")
    step3 = next(s for s in r["runbook"] if s["n"] == 3)
    assert "<chat_id>" in step3["run"][0]


def test_send_code():
    out = h.send_code("teolino", "48432")
    assert out["run"].endswith("session code teolino 48432")
    assert "--password <2FA>" in out["with_2fa"]


def test_send_code_validation():
    assert "error" in h.send_code("", "1")
    assert "error" in h.send_code("s", "")


def test_status_howto():
    out = h.status_howto("konex")
    assert "session list" in out["list_sessions"]
    assert "inbox/konex.ndjson" in out["monitor"]


def test_handle_dispatch():
    assert h.handle("tg_listen_activate", {"session": "a", "chat_id": "1"})["session"] == "a"
    assert h.handle("tg_listen_send_code", {"session": "a", "code": "9"})["run"]
    assert "list_sessions" in h.handle("tg_listen_status_howto", {})
    assert "error" in h.handle("nope", {})


def test_notes_mention_code_and_authkey():
    notes = " ".join(h.activate("a")["notes"])
    assert "Росен" in notes          # code comes from Rosen
    assert "authkey" in notes        # copy-kills-authkey warning
