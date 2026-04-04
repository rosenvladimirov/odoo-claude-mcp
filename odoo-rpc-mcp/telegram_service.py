"""
Telegram Client Integration via Telethon.

Provides messaging from the user's personal Telegram account.
Auth: API ID + Hash from my.telegram.org, then phone + code verification.
Session is saved for reuse.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("telegram-service")

TELEGRAM_CONFIG_FILE = Path(os.environ.get(
    "TELEGRAM_CONFIG_FILE", "/data/telegram_config.json"
))
TELEGRAM_SESSION_PATH = os.environ.get(
    "TELEGRAM_SESSION_PATH", "/data/telegram_session"
)


class TelegramServiceManager:
    """Manages Telegram client authentication and messaging."""

    def __init__(self):
        self._client = None
        self._api_id = None
        self._api_hash = None
        self._phone_code_hash = None
        self._load_config()

    def _load_config(self):
        # From environment
        self._api_id = os.environ.get("TELEGRAM_API_ID", "")
        self._api_hash = os.environ.get("TELEGRAM_API_HASH", "")

        # From config file
        if TELEGRAM_CONFIG_FILE.exists():
            try:
                data = json.loads(TELEGRAM_CONFIG_FILE.read_text())
                if not self._api_id:
                    self._api_id = str(data.get("api_id", ""))
                if not self._api_hash:
                    self._api_hash = data.get("api_hash", "")
            except Exception as e:
                logger.warning(f"Failed to load Telegram config: {e}")

        if self._api_id and self._api_hash:
            self._init_client()

    def _save_config(self):
        TELEGRAM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        TELEGRAM_CONFIG_FILE.write_text(json.dumps({
            "api_id": int(self._api_id),
            "api_hash": self._api_hash,
        }, indent=2))

    def _init_client(self):
        try:
            from telethon.sync import TelegramClient
            self._client = TelegramClient(
                TELEGRAM_SESSION_PATH,
                int(self._api_id),
                self._api_hash,
            )
            self._client.connect()
            if self._client.is_user_authorized():
                me = self._client.get_me()
                logger.info(
                    f"Telegram: authenticated as {me.first_name} "
                    f"(@{me.username or 'no username'})"
                )
            else:
                logger.info("Telegram: connected but not authorized (call telegram_auth)")
        except Exception as e:
            logger.warning(f"Telegram client init failed: {e}")
            self._client = None

    @property
    def is_authenticated(self) -> bool:
        return (
            self._client is not None
            and self._client.is_connected()
            and self._client.is_user_authorized()
        )

    def configure(self, api_id: str, api_hash: str) -> dict:
        """Set API credentials and initialize client."""
        self._api_id = str(api_id)
        self._api_hash = api_hash
        self._save_config()
        self._init_client()
        return {"status": "configured", "api_id": self._api_id}

    def auth_send_code(self, phone: str) -> dict:
        """Step 1: Send verification code to phone."""
        if not self._client:
            return {"status": "error", "message": "Not configured. Call telegram_configure first."}

        try:
            result = self._client.send_code_request(phone)
            self._phone_code_hash = result.phone_code_hash
            return {
                "status": "code_sent",
                "phone": phone,
                "message": "Verification code sent to Telegram. Call telegram_auth_verify with the code.",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def auth_verify(self, phone: str, code: str, password: str = "") -> dict:
        """Step 2: Verify with the code received."""
        if not self._client:
            return {"status": "error", "message": "Not configured."}

        try:
            self._client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=self._phone_code_hash,
            )
            me = self._client.get_me()
            return {
                "status": "authenticated",
                "user": me.first_name,
                "username": me.username or "",
                "phone": me.phone,
            }
        except Exception as e:
            err = str(e)
            if "Two-steps verification" in err or "password" in err.lower():
                if password:
                    try:
                        self._client.sign_in(password=password)
                        me = self._client.get_me()
                        return {
                            "status": "authenticated",
                            "user": me.first_name,
                            "username": me.username or "",
                            "phone": me.phone,
                        }
                    except Exception as e2:
                        return {"status": "error", "message": str(e2)}
                return {
                    "status": "2fa_required",
                    "message": "Two-factor authentication enabled. Call again with password parameter.",
                }
            return {"status": "error", "message": err}

    def auth_status(self) -> dict:
        if not self._client:
            return {"status": "not_configured"}
        if not self.is_authenticated:
            return {"status": "not_authenticated"}
        me = self._client.get_me()
        return {
            "status": "authenticated",
            "user": me.first_name,
            "username": me.username or "",
            "phone": me.phone,
        }

    def get_dialogs(self, limit: int = 20) -> list:
        """List recent chats/dialogs."""
        if not self.is_authenticated:
            raise Exception("Not authenticated. Call telegram_auth first.")

        dialogs = self._client.get_dialogs(limit=limit)
        return [
            {
                "id": d.id,
                "name": d.name,
                "type": (
                    "user" if d.is_user else
                    "group" if d.is_group else
                    "channel" if d.is_channel else "unknown"
                ),
                "unread_count": d.unread_count,
                "username": getattr(d.entity, "username", None) or "",
            }
            for d in dialogs
        ]

    def search_contacts(self, query: str) -> list:
        """Search contacts by name or username."""
        if not self.is_authenticated:
            raise Exception("Not authenticated.")

        from telethon.tl.functions.contacts import SearchRequest
        result = self._client(SearchRequest(q=query, limit=10))
        output = []
        for user in result.users:
            output.append({
                "id": user.id,
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "username": user.username or "",
                "phone": user.phone or "",
            })
        return output

    def get_messages(
        self, chat: str | int, limit: int = 10, search: str = ""
    ) -> list:
        """Read messages from a chat."""
        if not self.is_authenticated:
            raise Exception("Not authenticated.")

        entity = self._resolve_entity(chat)
        kwargs: dict[str, Any] = {"limit": limit}
        if search:
            kwargs["search"] = search

        messages = self._client.get_messages(entity, **kwargs)
        return [
            {
                "id": m.id,
                "date": m.date.isoformat() if m.date else "",
                "from": self._sender_name(m),
                "text": m.text or "",
                "media": bool(m.media),
            }
            for m in messages
        ]

    def send_message(self, chat: str | int, message: str, reply_to: int = 0) -> dict:
        """Send a message to a chat/user."""
        if not self.is_authenticated:
            raise Exception("Not authenticated.")

        entity = self._resolve_entity(chat)
        kwargs: dict[str, Any] = {}
        if reply_to:
            kwargs["reply_to"] = reply_to

        sent = self._client.send_message(entity, message, **kwargs)
        return {
            "status": "sent",
            "id": sent.id,
            "chat": str(chat),
            "date": sent.date.isoformat() if sent.date else "",
        }

    def _resolve_entity(self, chat: str | int):
        """Resolve a chat by username, phone, or ID."""
        if isinstance(chat, int) or (isinstance(chat, str) and chat.lstrip("-").isdigit()):
            return self._client.get_entity(int(chat))
        if chat.startswith("+"):
            return self._client.get_entity(chat)
        if not chat.startswith("@"):
            chat = f"@{chat}"
        return self._client.get_entity(chat)

    def _sender_name(self, message) -> str:
        sender = message.sender
        if sender is None:
            return "unknown"
        name = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        if last:
            name = f"{name} {last}".strip()
        username = getattr(sender, "username", "") or ""
        if username:
            return f"{name} (@{username})" if name else f"@{username}"
        return name or str(sender.id)
