"""Thin Centrifugo HTTP publish client (per-principal realtime push).

Publishes events to the realtime hub so subscribers (MCP wake-loop over SSE,
the hardware proxy over WS, or a human with websocat) receive them. Used by the
Telegram NewMessage handler (Phase 2b) to push ``telegram:<principal>`` events.

Stdlib-only and fail-soft: push is best-effort and never raises into the caller.
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("centrifugo-client")

# Internal docker-network address by default; overridable via env.
CENTRIFUGO_API_URL = os.environ.get(
    "CENTRIFUGO_API_URL", "http://centrifugo:8000/api/publish"
)
CENTRIFUGO_API_KEY = os.environ.get("CENTRIFUGO_API_KEY", "")


def is_configured() -> bool:
    return bool(CENTRIFUGO_API_KEY)


def publish(channel: str, data: dict, timeout: int = 5) -> bool:
    """POST an event to a Centrifugo channel. Returns True on success.

    Never raises — a down hub must not break the tool that triggered the push.
    """
    if not CENTRIFUGO_API_KEY:
        logger.debug("Centrifugo publish skipped: CENTRIFUGO_API_KEY not set")
        return False
    body = json.dumps({"channel": channel, "data": data}).encode("utf-8")
    req = urllib.request.Request(
        CENTRIFUGO_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": CENTRIFUGO_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 — best-effort push
        logger.warning(f"Centrifugo publish to '{channel}' failed: {e}")
        return False
