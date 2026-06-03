#!/usr/bin/env bash
# Centrifugo SSE wake-loop — emit ONE line per Telegram push event for a
# principal, so the agent's Monitor wakes and replies in near-real-time
# (replaces the hourly polling cron).
#
# Run under Monitor(persistent):
#   CENTRIFUGO_SSE_URL=https://centrifugo.mcpworks.net/connection/uni_sse TG_PRINCIPAL=rosen tools/tg_wakeloop.sh
#
# Token: provide TG_WAKE_TOKEN, or let the script mint one via centrifugo_token.py
# (requires CENTRIFUGO_TOKEN_HMAC_SECRET in env — i.e. run where the secret lives).
# The minted token carries a `channels` claim, so Centrifugo server-side-subscribes
# the connection to telegram:<principal> — no subscribe command needed.
#
# Endpoint MUST be the unidirectional /connection/uni_sse (needs uni_sse enabled in
# Centrifugo config). The bidirectional /connection/sse rejects a plain GET (3501).
set -euo pipefail

HUB="${CENTRIFUGO_SSE_URL:?set CENTRIFUGO_SSE_URL, e.g. https://centrifugo.mcpworks.net/connection/uni_sse}"
PRINCIPAL="${TG_PRINCIPAL:?set TG_PRINCIPAL (the MCP user id)}"
TOKEN="${TG_WAKE_TOKEN:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$TOKEN" ]; then
  TOKEN="$(python3 -c "import sys; sys.path.insert(0,'${HERE}/../odoo-rpc-mcp'); import centrifugo_token as t; print(t.mint_telegram_token('${PRINCIPAL}'))")"
fi

# Token-only connect request; the channels claim drives server-side subscription.
PAYLOAD="$(python3 -c "import json,urllib.parse; print(urllib.parse.quote(json.dumps({'token':'${TOKEN}'})))")"

# Each publication arrives as an SSE 'data:' frame carrying "channel":"telegram:..".
# Emit only those (skip the connect frame) so each becomes one Monitor notification.
exec curl -sN --max-time 0 "${HUB}?cf_connect=${PAYLOAD}" \
  | grep --line-buffered -E '"pub":|"channel":"telegram:' \
  | sed -u 's/^data: //'
