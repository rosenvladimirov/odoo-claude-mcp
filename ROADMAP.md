# Odoo Claude MCP — Ecosystem Roadmap

> Living document. Current stack: **v2.25.2** (prod, branch `2.0`) + **v3** (dev, `odoo-mcp-v3`, α-line).
> Theme of the current wave: **multi-user correctness + realtime push**.
> Order of work (per maintenance rule): land on **v2 first**, then **lockstep to v3** with an audit before push.

## Where we are

Docker stack (`docker-compose.yml`), networks `public` / `backend` / `cloudflare-net`:

| Service | Net | Role |
|---|---|---|
| `odoo-rpc-mcp` (gateway, :8084) | public+backend+cloudflare | XML/JSON-RPC tools, Telegram/Google/SSH, proxy gateway |
| `claude-terminal` (:8080) | public+backend | ttyd + Claude CLI, **multi-user** (per-user HOME dirs) |
| `portainer/teams/github/oca/ee/filesystem/backup` MCPs | backend | profile/optional toolsets |
| `qdrant` (:6333/6334) + `ollama` (:11434) | backend | AI tokenizer storage + local embeddings |

The multi-user pain shows up via `claude-terminal`: many human users share **one** `odoo-rpc-mcp` process.

---

## ⚠️ Root problem driving this wave: process-global active connection

The MCP keeps **one** live connection for the whole process, so a second user clobbers the first.

- Every Odoo tool resolves through `_conn()` → global `manager["default"]` — `odoo-rpc-mcp/server.py:1037` (v2), `:1269` (v3).
- `identify` / `odoo_connect` / `user_connection_activate` all cram the chosen connection into the shared `"default"` alias — v2 `:4589 / :4706 / :4809`.
- `OdooConnection` caches uid on the object (`:510`/`:618`) and `_web_sessions` is keyed by alias (`:1156`) → wrong identity bleeds across users.
- Telethon is a pure singleton — one client + one `/data/telegram_session` (`telegram_service.py:19-28`).
- Same class of bug: Google token (`/data/google_token.json`), and in v3 `tenant_router` (global `/data/active_tenant.json`) + `elevation` singleton.

**Binding scope = the Claude SESSION, not the user.** The active connection must be stored **per connected Claude (per MCP session)**, so two Claudes never mix connections — even two sessions of the *same* Odoo user (e.g. one on prod, one on test) stay independent.

- **Primary key — the MCP session** = `Mcp-Session-Id` header / `id(ctx.session)`; stable per connection because the transport is **stateful** Streamable HTTP (`stateless=False`).
- The verified HTTP principal in `_odoo_caller_ctx` (`mcp_user`, validated via XMLRPC, v2 `:8878`) is **not** the active-connection key — it stays as the session's *identity/permission* attribute and the default source of `connections.json` to hydrate from.
- **Never** the legacy `_session_users["current"]` — that shared slot is the bleed vector.
- Trade-off (accepted): a Claude that reconnects gets a fresh session → re-`identify`/re-connect (clean by design). Per-user `active_connection.json` is only the *default* seeded on session start; thereafter the live active connection lives in the session slot.

> **Two distinct scopes — don't conflate them:**
> - **Odoo active connection → per SESSION** (this fix). Each Claude picks its own DB/connection.
> - **Telegram / Google login → per USER (principal)** by nature — one human account, shared across that user's sessions. So `tg_registry` stays keyed by principal and Centrifugo channel `telegram:<principal>` legitimately pushes to all of that user's sessions.

---

## Phases

### Phase 1 — Per-session connection registry (the bug fix) — **v2 first**
Replace global active connection with `registry: {session_id → ConnectionContext}`, keyed by the **MCP session** (per connected Claude).
- `session_id = Mcp-Session-Id / id(ctx.session)`; resolved at top of `call_tool`. The session's principal (`mcp_user` / `identify` name) is stored *alongside* as identity, used only to pick which `connections.json` to read.
- `_conn()` becomes **session-aware** with **lazy hydrate**: on a session's first use, seed its active connection from that user's `active_connection.json` default; thereafter the slot is the session's own.
- The 3 "activate" sites (`odoo_connect` / `identify` / `user_connection_activate`) write into `registry[session_id]` instead of `manager["default"]` — so one Claude's `odoo_connect` never touches another's. Optionally also persist back as the user's default.
- Re-key `_web_sessions` to `(session_id, alias)`; lock the registry (`threading.Lock`); evict on session close / TTL.
- **~7 точкови edit-а на репо, огледални v2/v3.** Backward compatible (single connected Claude + `SINGLE_CONNECTION=true` unaffected; explicit `connection=` still overrides).

### Phase 2 — Per-user Telethon + event handler
- `tg_registry: {principal_id → TelegramClient}`, one session file per principal (`telegram_session_dir/<principal>.session`), per-principal api_id/api_hash and `_phone_code_hash`.
- One long-lived asyncio loop hosting all clients; each registers `@client.on(events.NewMessage)` bound to its principal.
- Handler publishes to Centrifugo channel `telegram:<principal>` (dedup by `msg_id`, debounce bursts ~5s).

### Phase 3 — Centrifugo push hub (Docker service in the stack)
- New `centrifugo` service in `docker-compose.yml`, on `backend` + `cloudflare-net` (external via Traefik/Cloudflare, TLS).
- Transports: **SSE + WebSocket** (WS reserved for future bidirectional hardware-proxy use). HTTP `POST /api/publish` (only `odoo-rpc-mcp` publishes).
- Auth: per-user **client JWT** (`sub=principal`, `channels=[telegram:<principal>]`) signed with `CENTRIFUGO_TOKEN_HMAC_SECRET`; publish guarded by `CENTRIFUGO_API_KEY` (`X-API-Key`). Namespace `telegram:`. Admin UI off by default.
- Secrets in gitignored `.env` (`openssl rand -hex 32`), placeholders in `.env.example`.

### Phase 4 — Client wake loop (replaces polling)
- `Monitor(persistent)` on the agent side → subscribe SSE (`curl -N`) or WS (`websocat`) to its principal channel → each event wakes the agent → read context → reply.
- The hourly storytelling cron drops to a fallback "tick" (or is retired).

### Phase 5 — Hardware-proxy & ecosystem integration (future)
- The ErpNet.FP / fiscal hardware proxy subscribes to its own Centrifugo channel(s) → bidirectional internal bus across the ecosystem, reusable by humans and devices (curl/websocat), not only MCP clients.
- Generalize channel scheme `<service>:<principal>[:<entity>]`.

### Parallel hardening wave (after Phase 1 proves out)
- Google token → per-user registry (same pattern).
- v3 `tenant_router` active-tenant + `elevation` → per-principal scope (RBAC, not connection — separate wave).
- Document the hard invariant: per-session fallback only holds while transport is `stateless=False`.
- Optional strongest identity: wire v3 `api_key_manager.verify` into the `/mcp` tool path → server-verified `key_id` principal.

---

## Decisions locked
- **Hub:** Centrifugo (Go, single Apache-2.0 binary, SSE+WS+HTTP-publish+JWT/namespaces+TLS). Runner-up Mercure (SSE-only, AGPL).
- **Active-connection scope:** the **Claude/MCP session** (`Mcp-Session-Id`) — NOT the user principal. Principal is a per-session identity/permission attribute only. This keeps two Claudes (even same user, e.g. prod vs test) from mixing connections.
- **Order:** v2 → lockstep v3 (+ audit before push).
- **Deploy:** Centrifugo as Docker service in the MCP stack, externally exposed.
- **Secrets:** two split secrets in `.env` (client-JWT HMAC + publish API key).

## Progress log
- **2026-06-03** — Phase 1 (per-session connection registry) DONE on v2 + v3, branch `feat/per-session-connection` (v2 `10b5062`, v3 `4cc4262`). Connection-isolation 7/7 on the live v2.25.2 container (202588745). v2↔v3 verified line-identical; v3-only modules (tenant_router/elevation/api_key_manager/provisioning) intact.
- **2026-06-03** — Phase 2a (per-principal Telegram registry) DONE on v2 + v3 (same commits). Local registry isolation 6/6.
- **2026-06-03** — Phase 3 scaffolding DONE on v2 + v3 (v2 `2e0d6f6`, v3 `1272ac0`): compose `centrifugo` service, `centrifugo/config.json` + README, `.env.example` secrets, `centrifugo_client.py` publish helper. **Not deployed.**
- **2026-06-03** — v3 + branch DEPLOYED to test stack 202588745 (host 164.68.114.107) as image `odoo-rpc-mcp-pstest:v3`; healthy, Restarts=0; isolation test 8/8 against live v3. Done all-ops "via bridge" (`ssh_execute`) — direct SSH:22 was fail2ban-blocked.
- **2026-06-03** — Phase 3 hub LIVE: `centrifugo` v6.8.1 container on 164.68.114.107 (networks cloudflare-net + odoo-claude-mcp_default), secrets in `/opt/centrifugo/secrets.env`. Dedicated Cloudflare tunnel `centrifugo-hub` (id 2503390f-1a12-48f8-ab65-139115384dbe) + connector container `centrifugo-cloudflared` → **public https://centrifugo.mcpworks.net** (mcpworks.net brand domain, NOT odoo-shell). Internal publish (MCP→centrifugo:8000) + public routing both verified. CF API token: provided by Rosen (full access).
- **Pending:** Phase 2b — wire MCP env (CENTRIFUGO_API_URL+CENTRIFUGO_API_KEY) + NewMessage handler (persistent asyncio loop) → publish telegram:&lt;principal&gt;; now testable against the live hub.

## Decisions locked (2026-06-03, cont.)
- **Centrifugo external exposure = Cloudflare tunnel** (always — house standard; stack already has `cloudflare-net` + a `cloudflared` container). No Traefik. Add a tunnel ingress hostname → `centrifugo:8000`.
- **Branches pushed** to `origin` (rosenvladimirov/odoo-claude-mcp): `feat/per-session-connection-2.0` and `feat/per-session-connection-3.0` (distinct names — same remote, divergent 2.0/3.0 histories). PRs open-able.

## Open items (need Rosen)
- On which host/stack to deploy the Centrifugo container + which Cloudflare tunnel + the ingress hostname (FQDN).
- REST `/api/connect` + `/api/identify` endpoints still clobber global `manager["default"]` (non-MCP surface) — fix too?
- Validate `centrifugo/config.json` keys (SSE/WS flags) against pinned `centrifugo/centrifugo:v6` tag before first `up`.
- Phase 2b loop-rewrite: only after a live hub exists to validate against (risk of breaking the sync model otherwise).
