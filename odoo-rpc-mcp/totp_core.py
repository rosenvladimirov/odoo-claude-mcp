"""
totp_core.py — RFC 6238 TOTP, чист stdlib (+ незадължителен segno за QR).

Едно място за математиката на втория фактор. Ползва се от:
- server.py   → identify(name) + identify_verify_totp (MCP name-identify, 3.3.0)
- admin_ui.py → вход в админ конзолата (3.3.8)

Тук НЯМА съхранение, криптиране, rate limit или replay guard — това остава
при извикващия, защото и двамата пазят тайната по различен начин (пътят до
профила, Fernet през MCP_KEY_PEPPER, брояч на опитите). Тук са само:
secret_new / code / verify / provisioning_uri / qr.

Съвместимост: изходът е байт-в-байт същият като помощниците, които живееха в
server.py до 3.3.7 (SHA1, 6 цифри, стъпка 30 s, прозорец ±1, otpauth URI с
issuer=OdooMCP) — иначе всяка вече записана тайна би спряла да сработва.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
import struct
import time
from urllib.parse import quote

STEP = 30          # секунди на стъпка (RFC 6238)
DIGITS = 6
WINDOW = 1         # приемаме ±1 стъпка за разминаване на часовниците
ISSUER = "OdooMCP"


def secret_new(nbytes: int = 20) -> str:
    """Нова base32 тайна без padding (20 байта = 160 бита, както препоръчва RFC 4226)."""
    return base64.b32encode(_secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def code(secret_b32: str, step: int, digits: int = DIGITS) -> str:
    """TOTP кодът за дадена стъпка (HMAC-SHA1, динамично отрязване по RFC 4226)."""
    pad = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    msg = struct.pack(">Q", int(step))
    dig = hmac.new(key, msg, hashlib.sha1).digest()
    off = dig[-1] & 0x0F
    truncated = struct.unpack(">I", dig[off:off + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** digits)).zfill(digits)


def verify(secret_b32: str, user_code: str, at: float | None = None,
           window: int = WINDOW, step: int = STEP, digits: int = DIGITS):
    """Връща (ok, съвпаднала_стъпка). Кодът се нормализира (интервали махнати);
    всичко, което не е точно `digits` цифри, е отказ без изчисление."""
    user_code = (user_code or "").strip().replace(" ", "")
    if not user_code.isdigit() or len(user_code) != digits:
        return False, None
    now = time.time() if at is None else at
    now_step = int(now) // step
    for w in range(-window, window + 1):
        st = now_step + w
        if hmac.compare_digest(code(secret_b32, st, digits), user_code):
            return True, st
    return False, None


def provisioning_uri(label: str, secret_b32: str, issuer: str = ISSUER,
                     digits: int = DIGITS, step: int = STEP) -> str:
    """otpauth:// адрес за authenticator приложението. `label` е видимото име
    на записа (примерно „OdooMCP:rosen“); кодира се с quote(), както преди."""
    return (f"otpauth://totp/{quote(label)}?secret={secret_b32}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={digits}&period={step}")


def qr_unicode(qr) -> str:
    """Матрицата на segno като текстов QR с половин блокове (2 модула на ред),
    с тиха зона. Чете се в терминал и в чат; сканира се най-добре на светъл фон."""
    rows = [list(r) for r in qr.matrix]
    b = 2  # тиха зона в модули
    width = len(rows[0]) + 2 * b
    blank = [0] * width
    grid = ([blank[:] for _ in range(b)]
            + [[0] * b + list(r) + [0] * b for r in rows]
            + [blank[:] for _ in range(b)])
    if len(grid) % 2:
        grid.append(blank[:])
    lines = []
    for y in range(0, len(grid), 2):
        top, bot = grid[y], grid[y + 1]
        line = []
        for x in range(width):
            t, d = top[x], bot[x]
            line.append("█" if (t and d) else "▀" if t else "▄" if d else " ")
        lines.append("".join(line))
    return "\n".join(lines)


def qr(uri: str) -> dict:
    """QR на otpauth адреса: `qr_ascii` (терминал/чат) + `qr_svg` (data URI за
    браузър). Празен dict, ако segno липсва — записването работи и без QR,
    през тайната / адреса."""
    try:
        import segno
    except Exception:  # noqa: BLE001
        return {}
    try:
        m = segno.make(uri, error="m")
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    try:
        out["qr_ascii"] = qr_unicode(m)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["qr_svg"] = m.svg_data_uri(scale=6, border=4)
    except Exception:  # noqa: BLE001
        pass
    return out
