"""Per-principal Telegram chat subscription allow-list.

Admin (Rosen) назначава кои чатове да слуша всеки principal, така че всеки
Claude получава в своя ``telegram:<principal>`` канал само СВОИТЕ чатове.

Политика (избрана): **allow-all back-compat**
  * няма файл / празен списък  → ``allowed_chats`` връща None ⇒ публикувай ВСИЧКО
  * непразен списък            → set от enabled chat_id-та ⇒ филтрирай само тях

Файлът е per-principal: ``<user_dir>/telegram_subscriptions.json`` (за
``__global__`` → ``$DATA_DIR/telegram_subscriptions.json``). Чете се при всяко
съобщение, така че промените влизат веднага без рестарт.
"""
import json
import os
from datetime import datetime, timezone


def _load(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.loads(fh.read())
    except Exception:
        return None


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def allowed_chats(path):
    """Set от enabled chat_id-та, или None за allow-all (празно/липсва)."""
    data = _load(path)
    if not data:
        return None
    subs = data.get("subscriptions") or []
    enabled = {int(s["chat_id"]) for s in subs if s.get("enabled", True)}
    return enabled or None  # празен allow-list → None → allow-all (back-compat)


def list_subs(path):
    data = _load(path) or {}
    return data.get("subscriptions", [])


def add_sub(path, chat_id, title="", note="", mode="auto"):
    data = _load(path) or {"subscriptions": []}
    subs = data.setdefault("subscriptions", [])
    for s in subs:
        if int(s["chat_id"]) == int(chat_id):
            s.update({"title": title or s.get("title", ""),
                      "note": note or s.get("note", ""),
                      "mode": mode, "enabled": True})
            break
    else:
        subs.append({"chat_id": int(chat_id), "title": title, "note": note,
                     "mode": mode, "enabled": True,
                     "added_at": datetime.now(timezone.utc).isoformat()})
    _save(path, data)
    return {"ok": True, "chat_id": int(chat_id), "count": len(subs)}


def remove_sub(path, chat_id):
    data = _load(path) or {"subscriptions": []}
    subs = [s for s in data.get("subscriptions", [])
            if int(s["chat_id"]) != int(chat_id)]
    data["subscriptions"] = subs
    _save(path, data)
    return {"ok": True, "removed": int(chat_id), "count": len(subs)}


def set_enabled(path, chat_id, enabled):
    data = _load(path) or {"subscriptions": []}
    hit = False
    for s in data.get("subscriptions", []):
        if int(s["chat_id"]) == int(chat_id):
            s["enabled"] = bool(enabled)
            hit = True
    if hit:
        _save(path, data)
    return {"ok": hit}
