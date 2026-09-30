"""telegram_create_group — група в Telegram от акаунта на човека.

Bot API не може да създава групи; създава ги само потребителски (MTProto)
акаунт. Затова групата за консултация се прави от акаунта на Росен през
telethon сесията на MCP, под негов надзор (ADR telegram-consult-bot/0002).

Два режима:
  * ``request_id`` — заявка ``l10n.bg.telegram.consult.request`` от Odoo
    (модул ``l10n_bg_telegram_consult``): заглавието, ботът и членовете идват
    от ``l10n_bg_consult_payload``; след създаването чатът и линкът се връщат
    в заявката с ``l10n_bg_set_group`` и ботът праща поканата на клиента.
    Odoo отказва неодобрена заявка ПРЕДИ групата да е създадена.
  * без ``request_id`` — само група от ``title``/``members``/``bot_username``.
"""
from __future__ import annotations

REQUEST_MODEL = "l10n.bg.telegram.consult.request"


def _members(raw) -> list[str]:
    """Членовете като списък от @имена; приема и низ, разделен с интервали/запетаи."""
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return [str(m).strip() for m in (raw or []) if str(m).strip()]


def run(tg, conn, args: dict) -> dict:
    request_id = args.get("request_id")
    if request_id:
        if conn is None:
            return {"error": "no_odoo_connection",
                    "hint": "request_id needs an active Odoo connection."}
        # Първо Odoo: неодобрена/чужда заявка гърми тук, преди да има група
        payload = conn.execute_kw(
            REQUEST_MODEL, "l10n_bg_consult_payload", [[int(request_id)]])[0]
        title = args.get("title") or payload["title"]
        bot_username = args.get("bot_username") or payload.get("bot_username") or ""
        members = _members(args.get("members") or payload.get("members"))
    else:
        title = (args.get("title") or "").strip()
        if not title:
            return {"error": "title_required",
                    "hint": "Pass title (and members), or request_id from Odoo."}
        bot_username = args.get("bot_username") or ""
        members = _members(args.get("members"))

    result = tg.create_group(
        title=title, members=members, bot_username=bot_username,
        about=args.get("about", ""),
    )
    if request_id:
        conn.execute_kw(
            REQUEST_MODEL, "l10n_bg_set_group",
            [[int(request_id)], result["chat_id"], result["invite_link"]])
        result["request_id"] = int(request_id)
        result["odoo"] = "group linked; the bot sent the invitation to the client"
    return result
