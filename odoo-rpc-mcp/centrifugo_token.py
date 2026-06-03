"""Mint Centrifugo connection JWTs (HS256), stdlib-only — no PyJWT dependency.

A subscriber (the MCP wake-loop, the hardware proxy, a human) presents this
token to Centrifugo. The token's ``sub`` is the principal and ``channels``
scopes it to that principal's channel(s) only — so a user can never subscribe
to another user's events. Signed with CENTRIFUGO_TOKEN_HMAC_SECRET (same secret
Centrifugo verifies with).
"""
import base64
import hashlib
import hmac
import json
import os
import time

TOKEN_SECRET = os.environ.get("CENTRIFUGO_TOKEN_HMAC_SECRET", "")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_token(sub: str, channels: list[str] | None = None,
               ttl_seconds: int = 3600, secret: str | None = None,
               now: int | None = None) -> str:
    """Return a signed HS256 JWT for a Centrifugo connection.

    sub          — principal id (Centrifugo user identity)
    channels     — channels the token may subscribe to (server-side scoping)
    ttl_seconds  — token lifetime; subscriber must refresh before expiry
    """
    key = secret if secret is not None else TOKEN_SECRET
    if not key:
        raise RuntimeError("CENTRIFUGO_TOKEN_HMAC_SECRET not set — cannot mint token")
    iat = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": str(sub), "iat": iat, "exp": iat + int(ttl_seconds)}
    if channels:
        payload["channels"] = list(channels)
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")
    sig = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return signing_input.decode("ascii") + "." + _b64url(sig)


def mint_telegram_token(principal: str, ttl_seconds: int = 3600) -> str:
    """Convenience: token scoped to a principal's telegram channel."""
    return mint_token(principal, channels=[f"telegram:{principal}"], ttl_seconds=ttl_seconds)
