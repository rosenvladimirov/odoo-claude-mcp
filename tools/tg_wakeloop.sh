#!/usr/bin/env bash
# Centrifugo SSE wake-loop — emit ONE line per Telegram push event for a
# principal, so the agent's Monitor wakes and replies in near-real-time
# (replaces the hourly polling cron).
#
# Run under Monitor(persistent):
#   CENTRIFUGO_SSE_URL=https://<hub>/connection/sse TG_PRINCIPAL=rosen tools/tg_wakeloop.sh
#
# Token: provide TG_WAKE_TOKEN, or let the script mint one via centrifugo_token.py
# (requires CENTRIFUGO_TOKEN_HMAC_SECRET in env — i.e. run where the secret lives).
#
# NOTE: validate the SSE framing (cf_connect query + "data:" lines) against the
# pinned centrifugo/centrifugo:v6 image before relying on this in production.
set -euo pipefail

HUB="${CENTRIFUGO_SSE_URL:?set CENTRIFUGO_SSE_URL, e.g. https://hub.example.com/connection/sse}"
PRINCIPAL="${TG_PRINCIPAL:?set TG_PRINCIPAL (the MCP user id)}"
TOKEN="${TG_WAKE_TOKEN:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$TOKEN" ]; then
  TOKEN="$(python3 -c "import sys; sys.path.insert(0,'${HERE}/../odoo-rpc-mcp'); import centrifugo_token as t; print(t.mint_telegram_token('${PRINCIPAL}'))")"
fi

CHAN="telegram:${PRINCIPAL}"
PAYLOAD="$(python3 -c "import json,urllib.parse; print(urllib.parse.quote(json.dumps({'token':'${TOKEN}','subs':{'${CHAN}':{}}})))")"

# Each push arrives as an SSE 'data:' frame; emit the JSON payload line so each
# becomes a single Monitor notification.
exec curl -sN --max-time 0 "${HUB}?cf_connect=${PAYLOAD}" \
  | grep --line-buffered -E '"channel":"telegram:' \
  | sed -u 's/^data: //'
