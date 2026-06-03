# Centrifugo — realtime push hub (SSE + WebSocket)

Sidecar that fans out per-principal events (e.g. `telegram:<principal>`) to
subscribers — the MCP wake-loop (`curl -N` SSE), the hardware proxy (WS), or a
human with `websocat`. MCP publishes via the HTTP API; subscribers connect with
a backend-issued per-user JWT scoped to their channel(s).

## Secrets (env, gitignored)

`config.json` ships placeholders — the real values come from env (env overrides
file in Centrifugo):

- `CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY` ← `${CENTRIFUGO_TOKEN_HMAC_SECRET}` — signs per-user client JWTs.
- `CENTRIFUGO_HTTP_API_KEY` ← `${CENTRIFUGO_API_KEY}` — guards `POST /api/publish` (only the MCP publishes).

Generate: `openssl rand -hex 32` for each.

## Channels

`telegram:<principal>` — one channel per MCP user. The connection JWT carries
`sub=<principal>` and `channels=["telegram:<principal>"]`, so a subscriber only
ever receives its own user's events.

## Subscribe / publish (validate against the pinned image first)

```bash
# Publish (MCP backend)
curl -H "X-API-Key: $CENTRIFUGO_API_KEY" -X POST \
  -d '{"channel":"telegram:rosen","data":{"event":"new_message","chat":"@smartsysbg"}}' \
  https://<hub-host>/api/publish

# Subscribe SSE (MCP wake-loop / browser / device)
curl -N "https://<hub-host>/connection/sse?cf_connect=$(printf '{"token":"<jwt>","subs":{"telegram:rosen":{}}}' | jq -sRr @uri)"

# Subscribe WS (hardware proxy / websocat)
websocat wss://<hub-host>/connection/websocket
# then: {"connect":{"token":"<jwt>"},"id":1} / {"subscribe":{"channel":"telegram:rosen"},"id":2}
```

> ⚠️ **Before `docker compose up`:** validate `config.json` keys + the SSE/WS
> transport flags against the pinned `centrifugo/centrifugo` image tag — the v6
> config schema differs from v5. Also decide external exposure (Cloudflare
> tunnel vs Traefik) + TLS — see ROADMAP "Open items".
