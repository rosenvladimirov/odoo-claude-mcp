"""Тестове за telegram_create_group (telegram_group.py + TelegramServiceManager.create_group).

Групата се прави от акаунта на човека (MTProto) — Bot API не може. Тук няма
истински Telegram: FakeTg/FakeConn пазят реда на извикванията, а за самото
създаване на групата telethon е подменен с фалшиви класове (локално telethon
дори не е инсталиран).

Пускане от корена на odoo-rpc-mcp:
    pytest tests/test_telegram_group.py -v
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_group as tgg  # noqa: E402

PAYLOAD = {
    "id": 7,
    "name": "TGC/2026/0007",
    "title": "TGC/2026/0007 — Ivan Petrov",
    "bot_username": "OdooShellConsultBot",
    "members": ["anna", "ivanp"],
}


class FakeConn:
    def __init__(self, refuse=False):
        self.calls = []
        self.refuse = refuse

    def execute_kw(self, model, method, args, kwargs=None):
        self.calls.append((model, method, args))
        if method == "l10n_bg_consult_payload":
            if self.refuse:
                raise Exception("UserError: TGC/2026/0007 is New; this step is not possible.")
            return [dict(PAYLOAD)]
        return True


class FakeTg:
    def __init__(self, log):
        self.log = log
        self.kwargs = None

    def create_group(self, **kwargs):
        self.log.append("create_group")
        self.kwargs = kwargs
        return {"status": "created", "chat_id": "-1001234", "invite_link": "https://t.me/+abc",
                "added": kwargs["members"], "failed": [], "bot_added": True, "title": kwargs["title"]}


class LoggingConn(FakeConn):
    def __init__(self, log, **kw):
        super().__init__(**kw)
        self.log = log

    def execute_kw(self, model, method, args, kwargs=None):
        self.log.append(method)
        return super().execute_kw(model, method, args, kwargs)


# --- оркестрация -----------------------------------------------------------

def test_request_reads_payload_creates_group_then_links_it():
    log = []
    conn, tg = LoggingConn(log), FakeTg(log)
    result = tgg.run(tg, conn, {"request_id": 7})
    assert log == ["l10n_bg_consult_payload", "create_group", "l10n_bg_set_group"]
    assert tg.kwargs["title"] == PAYLOAD["title"]
    assert tg.kwargs["bot_username"] == "OdooShellConsultBot"
    assert tg.kwargs["members"] == ["anna", "ivanp"]
    assert conn.calls[-1] == (
        tgg.REQUEST_MODEL, "l10n_bg_set_group", [[7], "-1001234", "https://t.me/+abc"])
    assert result["request_id"] == 7


def test_unapproved_request_creates_no_group():
    log = []
    conn, tg = LoggingConn(log, refuse=True), FakeTg(log)
    with pytest.raises(Exception, match="not possible"):
        tgg.run(tg, conn, {"request_id": 7})
    assert "create_group" not in log


def test_explicit_arguments_override_the_request():
    log = []
    tg = FakeTg(log)
    tgg.run(tg, FakeConn(), {"request_id": 7, "title": "Custom", "members": "@x, @y"})
    assert tg.kwargs["title"] == "Custom"
    assert tg.kwargs["members"] == ["@x", "@y"]


def test_request_without_connection_is_an_error_not_a_group():
    log = []
    result = tgg.run(FakeTg(log), None, {"request_id": 7})
    assert result["error"] == "no_odoo_connection"
    assert log == []


def test_plain_group_needs_a_title_and_does_not_touch_odoo():
    log = []
    assert tgg.run(FakeTg(log), None, {})["error"] == "title_required"
    result = tgg.run(FakeTg(log), None, {"title": "Team", "members": ["@a"]})
    assert result["chat_id"] == "-1001234"
    assert log == ["create_group"]


# --- TelegramServiceManager.create_group с подменен telethon ----------------

class _Request:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class RPCError(Exception):
    pass


class UserPrivacyRestrictedError(RPCError):
    pass


@pytest.fixture
def fake_telethon(monkeypatch):
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("telethon")
    module("telethon.errors", RPCError=RPCError)
    module("telethon.tl")
    module("telethon.tl.functions")
    module("telethon.tl.functions.channels",
           CreateChannelRequest=type("CreateChannelRequest", (_Request,), {}),
           InviteToChannelRequest=type("InviteToChannelRequest", (_Request,), {}))
    module("telethon.tl.functions.messages",
           ExportChatInviteRequest=type("ExportChatInviteRequest", (_Request,), {}))


class FakeClient:
    """Отговаря на заявките като Telegram: блокиран потребител, missing_invitees."""

    def __init__(self):
        self.requests = []
        self.admin = None
        self.channel = types.SimpleNamespace(id=555)

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "CreateChannelRequest":
            return types.SimpleNamespace(chats=[self.channel])
        if name == "InviteToChannelRequest":
            user = request.args[1][0]
            if user == "blocked":
                raise UserPrivacyRestrictedError("privacy")
            missing = ["x"] if user == "hidden" else []
            return types.SimpleNamespace(missing_invitees=missing)
        if name == "ExportChatInviteRequest":
            return types.SimpleNamespace(link="https://t.me/+xyz")
        raise AssertionError(name)

    async def edit_admin(self, channel, user, **rights):
        self.admin = (user, rights)

    async def get_entity(self, key):
        return {"@OdooShellConsultBot": "bot", "@anna": "anna",
                "@blocked": "blocked", "@hidden": "hidden"}[key]


def _manager(client):
    import telegram_service as ts

    class Manager(ts.TelegramServiceManager):
        is_authenticated = True

        def _run(self, coro, timeout=60):
            return asyncio.run(coro)

    mgr = Manager.__new__(Manager)
    mgr._client = client
    return mgr


def test_service_creates_supergroup_with_bot_admin_and_reports_failures(fake_telethon):
    client = FakeClient()
    result = _manager(client).create_group(
        title="TGC/2026/0007", members=["anna", "blocked", "hidden"],
        bot_username="OdooShellConsultBot")
    create = client.requests[0]
    assert type(create).__name__ == "CreateChannelRequest"
    assert create.kwargs["megagroup"] is True
    assert client.admin[0] == "bot"
    assert client.admin[1]["is_admin"] is True and client.admin[1]["invite_users"] is True
    assert result["chat_id"] == "-100555"
    assert result["invite_link"] == "https://t.me/+xyz"
    assert result["added"] == ["anna"]
    assert result["failed"] == [
        {"member": "blocked", "reason": "UserPrivacyRestrictedError"},
        {"member": "hidden", "reason": "privacy_restricted"},
    ]
