"""
v3 Telegram-listen helper — returns a precise RUNBOOK for the LOCAL Claude to
bring up its tg-listener container and start differentiated listening.

Design (per Rosen): the MCP server does NOT reach into the laptop. It hands the
caller (a Claude running on the laptop, which has its own docker/bash) the exact
commands to self-activate a listening session — enroll the session+chat, log it
in, send the Telegram-generated code, arm its Monitor. Each Claude manages its
OWN session → differentiated listening (one chat → one Claude's inbox).

Pure instruction generation — touches nothing, no infra calls, no secrets
embedded (api creds are referenced by file path, the login code comes from
Rosen). Reachable by any authenticated principal (control plane).
"""

from __future__ import annotations

import os

LISTENER_DIR = os.environ.get("TG_LISTENER_DIR", "~/odoo-claude-connections/tg_listener")
CONFIG_FILE = os.environ.get("TG_CONFIG_FILE", "~/odoo-claude-connections/telegram_config.json")
DEFAULT_PHONE = os.environ.get("TG_DEFAULT_PHONE", "+359886100204")


def _ct(cmd: str) -> str:
    return f"docker exec tg-listener python -m app.tgctl {cmd}"


def activate(session: str, chat_id: str = "", title: str = "",
             phone: str = "", fresh_login: bool = True) -> dict:
    """Return the runbook for the local Claude to start listening to `chat_id`
    under its own `session`. Differentiated: only this session's inbox receives
    the chat. The Telegram login code is sent in step 4 (ask Rosen for it)."""
    if not session:
        return {"error": "session is required (your listener session id, e.g. 'teolino')"}
    phone = phone or DEFAULT_PHONE
    sub_cmd = _ct(f"sub add {session} {chat_id}") + (f"   # {title}" if title else "") \
        if chat_id else "# (no chat_id given — add later with: " + _ct(f"sub add {session} <chat_id>") + ")"
    login_step = (
        _ct(f"session add {session} --api-id <ID> --api-hash <HASH>")
        + f"   # creds from {CONFIG_FILE} (NOT --from-master — copies kill authkeys)"
    ) if fresh_login else _ct(f"session add {session} --from-master")

    steps = [
        {"n": 1, "what": "Контейнерът да върви",
         "run": [f"cd {LISTENER_DIR} && docker compose up -d",
                 "docker ps --filter name=tg-listener --format '{{.Status}}'"]},
        {"n": 2, "what": "Създай своята сесия (fresh login, не копие)",
         "run": [login_step]},
        {"n": 3, "what": "Абонирай своя чат (диференцирано — само твоят inbox)",
         "run": [sub_cmd]},
        {"n": 4, "what": "Login + код от Telegram (поискай кода от Росен)",
         "run": [_ct(f"session login {session} --phone {phone}"),
                 "# Telegram праща код → поискай го от Росен, после:",
                 _ct(f"session code {session} <КОД>")]},
        {"n": 5, "what": "Провери LIVE",
         "run": [f"docker logs tg-listener --since 30s 2>&1 | grep \"session '{session}' LIVE\""]},
        {"n": 6, "what": "Въоръжи своя Monitor (persistent)",
         "run": [f"tail -n0 -F {LISTENER_DIR}/inbox/{session}.ndjson"]},
        {"n": 7, "what": "Обработи backlog-а",
         "run": [_ct(f"messages --session {session} --unprocessed")]},
    ]
    return {
        "session": session, "chat_id": chat_id or None, "title": title or None,
        "phone": phone,
        "runbook": steps,
        "notes": [
            "Кодът от Telegram го има само Росен — не можеш да го вземеш сам.",
            "Fresh login (не --from-master): копие на сесия → Telegram убива "
            "authkey на всички клонинги.",
            "Само твоята сесия получава този чат → друг Claude не го вижда.",
            "Liveness: ако логът мълчи без 'LIVE', supervisor-ът е увиснал → "
            "`docker restart tg-listener`.",
        ],
    }


def send_code(session: str, code: str) -> dict:
    """Return the exact command to complete a pending login with the Telegram
    code (and the optional 2FA-password variant)."""
    if not session or not code:
        return {"error": "session and code are required"}
    return {
        "session": session,
        "run": _ct(f"session code {session} {code}"),
        "with_2fa": _ct(f"session code {session} {code} --password <2FA>"),
        "verify": f"docker logs tg-listener --since 20s 2>&1 | grep \"session '{session}' LIVE\"",
    }


def status_howto(session: str = "") -> dict:
    """How the local Claude checks listener / session state."""
    return {
        "list_sessions": _ct("session list"),
        "list_subs": _ct(f"sub list {session}") if session else _ct("sub list <session>"),
        "live_check": "docker logs tg-listener --tail 40 2>&1 | grep -E \"LIVE|authorized|ВНИМАНИЕ\"",
        "backlog": _ct(f"messages --session {session or '<session>'} --unprocessed"),
        "monitor": f"tail -n0 -F {LISTENER_DIR}/inbox/{session or '<session>'}.ndjson",
    }


# ─── registration (control plane — any authenticated principal) ───────────

CONTROL_TOOL_NAMES = {"tg_listen_activate", "tg_listen_send_code",
                      "tg_listen_status_howto"}


def get_control_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="tg_listen_activate",
             description=("Return a precise runbook for THIS (local) Claude to "
                          "bring up its tg-listener container and start listening "
                          "to a chat under its own session — enroll, login, send "
                          "the Telegram code, arm its Monitor. Differentiated: "
                          "only this session's inbox receives the chat. The tool "
                          "does NOT execute — the local Claude runs the steps."),
             inputSchema={"type": "object", "properties": {
                 "session": {"type": "string",
                             "description": "your listener session id, e.g. 'teolino'"},
                 "chat_id": {"type": "string",
                             "description": "Telegram chat id (group=-100.., user=numeric)"},
                 "title": {"type": "string"},
                 "phone": {"type": "string"},
                 "fresh_login": {"type": "boolean", "default": True}},
                 "required": ["session"]}),
        Tool(name="tg_listen_send_code",
             description=("Return the exact tgctl command to complete a pending "
                          "session login with the Telegram-generated code (ask "
                          "Rosen for the code)."),
             inputSchema={"type": "object", "properties": {
                 "session": {"type": "string"}, "code": {"type": "string"}},
                 "required": ["session", "code"]}),
        Tool(name="tg_listen_status_howto",
             description="Return the commands to inspect listener/session state.",
             inputSchema={"type": "object",
                          "properties": {"session": {"type": "string"}}}),
    ]


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "tg_listen_activate":
        return activate(arguments.get("session", ""),
                        arguments.get("chat_id", ""),
                        arguments.get("title", ""),
                        arguments.get("phone", ""),
                        arguments.get("fresh_login", True))
    if name == "tg_listen_send_code":
        return send_code(arguments.get("session", ""), arguments.get("code", ""))
    if name == "tg_listen_status_howto":
        return status_howto(arguments.get("session", ""))
    return {"error": f"unknown tg_listen tool: {name}"}
