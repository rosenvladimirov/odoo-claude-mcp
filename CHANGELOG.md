# Changelog

All notable changes to the Odoo RPC MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — `mcp_terminal_get_config` (port от v2.25.1+v2.25.2)
- Tool четеше несъществуващи env vars (`MCP_CLIENT_TOKEN`, `MCP_API_KEY`,
  `MCP_PUBLIC_URL`) → ZIP-овете излизаха с празни ключове за всеки tenant.
- Сега чете реалните deployment env names (`MCP_SECRET_TOKEN`,
  `MCP_ADMIN_TOKEN`, `MCP_OAUTH_CLIENT_ID`), с legacy per-tenant overrides
  като escape hatch и Cloudflare DNS (`mcp-{slug}.mcpworks.net` /
  `terminal-{slug}.mcpworks.net`) като auto-derived default.
- Нов `_env_chain()` helper различава "env unset" (`None`) от "explicitly
  empty" (`""`) — позволява `CLAUDE_TERMINAL_URL=` в compose да потуши
  auto-derive (за VPN-only deploy-и без публичен terminal host).
- `include_anthropic` default `True → False` (privacy: Anthropic API key
  е per-user/sensitive, не се embedva автоматично в onboarding ZIP).
- Vendored е същият fix както в v2.25.2 (commits `612b6aa` + `501ffbc`
  на 2.0 branch). v3 endpoint все още не е в production usage.

### Added — v3 active tenant routing
- New module `odoo-rpc-mcp/tenant_router.py`:
  - 4 control plane tools: `tenant_list`, `tenant_use`, `tenant_current`,
    `tenant_refresh`
  - Persistent state: `/data/active_tenant.json` (atomic replace + 0o600,
    same pattern as `admin_ui.py` ADMIN_CONFIG)
  - Lazy per-tenant discovery cache + health snapshot
  - Always-on tenants via `TENANT_ALWAYS_ON` env (default: `main`)
- `server.py` integration:
  - `_discover_one(name)` helper for targeted discovery
  - `list_tools()` filters proxy tools to `always_on` + `active_tenant` +
    control plane (~278 tools instead of 1028)
  - `call_tool()` dispatches `tenant_*` tools before generic proxy
  - `_startup_discover()` eager only for always-on; restores active tenant
    from disk on boot
  - SSE init declares `NotificationOptions(tools_changed=True)` for
    explicit capability advertisement
- `tenant_use(name)` emits `notifications/tools/list_changed` so Claude Code
  re-fetches the tool list without reconnect
- 9/9 standalone unit tests pass for `tenant_router` in isolation

## [3.0.0-alpha] — 2026-04-28 — v3 kickoff: developer/integrator gateway

v3 е изцяло dev/integration ориентиран. Целта: един MCP endpoint, който
прозрачно проксира към всички runtime v2.x stacks, плюс по-късно собствени
client install/lifecycle tools и skills bundle.

### Added
- `proxy_services.v3.example.json` — 7 remote v2.x targets (main + 6 client стака)
  с `${MCP_TOKEN_*}` env expansion. Exploits existing `transport: http` +
  `streamablehttp_client` proxy без нов код.
- `V3_TRANSPARENT_PROXY.md` — bootstrap workflow (SSH еднократно за tokens)
  + tenant config през `mcp_terminal_get_config` proxy chain.

### Removed (v3 scope cleanup)
- `claude-terminal/` — v3 е dev tool, не end-user web терминал.
- `teams-mcp/` — Microsoft Teams не е integration scope.
- Свързани services/volumes от `docker-compose.yml` + `docker-compose.prod-pins.yml`.

### Notes
- `odoo-rpc-mcp/server.py` references към `l10n_bg_claude_terminal` (Odoo
  модул, който генерира config bundle) са запазени — ползват се от
  `mcp_terminal_get_config`.
- Validated на 2026-04-28: 7 remote endpoints (122 + 6×151 = 1028 prefixed
  tools). Active tenant routing предстои за token cost reduction.

## [2.24.0] — 2026-04-24 — Final 2.x polish: admin managers, HTTP auth, metrics scaffold

This is the **final minor on the 2.x track** before production freeze.
Six phases shipped together.

### Added — Phase 1: Backup Manager (`/admin/backups`)
- New module `admin_backup.py` wired into `admin_ui.get_routes()` via an
  extension mechanism. Nav links added to `admin_ui.py`.
- Per-tenant scope: main admin sees every `mcp-backup-*` bucket; client
  admins see only their own `mcp-backup-<CLIENT_ID>`.
- UI: tabbed bucket switcher, stats bar, day-grouped file list, inline
  JSON viewer, bulk delete-prefix, ZIP export, retention editor modal.
- Destructive ops require the `X-Admin-Rechallenge` header with the
  admin's password (re-verified against the user auth store).
- Auto-rotation:
  - Config in `/shared-data/backup_rotation.json` (or
    `BACKUP_ROTATION_CONFIG` env override). Per-bucket `keep_days` +
    `min_objects`, daily `run_at`, IANA `timezone`, defaults.
  - `rotate_once(dry_run=False)` applies policy across allowed buckets.
  - `start_scheduler()` installs an APScheduler cron (silent no-op if
    APScheduler missing — manual rotation stays functional).
  - Log file at `/shared-data/backup_rotation.log` + audit table.

### Added — Phase 2: Filestore Manager (`/admin/filestore`)
- New module `admin_filestore.py` — browse/edit the `/shared-data`
  volume (sandbox root configurable via `SHARED_DATA_ROOT`). Tree +
  detail UI, breadcrumbs, inline textarea editor for common extensions
  (`.md .json .yml .yaml .xml .py .sh .html .css .js .csv .ini .toml
  .conf .log`), image preview for PNG/JPG/WEBP/GIF/SVG/BMP.
- Full CRUD: list, read, write, upload (multipart, `ADMIN_FS_MAX_UPLOAD_MB`
  default 50), rm, mkdir, mv. Path-traversal protection via
  `Path.resolve()` + `relative_to(SANDBOX_ROOT)` guard.
- `ADMIN_FS_READONLY=1` env flag disables all destructive ops; readonly
  badge shown in UI.
- Destructive ops require `X-Admin-Rechallenge` (same pattern as Phase 1).

### Security — Phase 3: HTTP auth on `/mcp`
- Today's discovery: `http://poligroup:8084/mcp` accepted every request
  with no auth header, because the old enforcement skipped requests that
  didn't carry `X-Odoo-*` when `MCP_SECRET_TOKEN` was empty.
- Now: new env var `MCP_REQUIRE_AUTH` (default `1`). When set the server
  rejects protected paths unconditionally if the caller presents no
  credentials — regardless of whether `MCP_SECRET_TOKEN` is configured.
- Startup warning if `MCP_REQUIRE_AUTH=1` and `MCP_SECRET_TOKEN` is
  empty (every request will 401 until the token is set).
- `/health` and `/metrics` stay open (load balancer probes, scrape).

### Added — Phase 4: plugin version pins (no `:latest` in production)
- Built + pushed 1.0.0 semver tags to Docker Hub:
  - `vladimirovrosen/odoo-filesystem-mcp:1.0.0`
  - `vladimirovrosen/odoo-portainer-mcp:1.0.0`
  - `vladimirovrosen/odoo-oca-mcp:1.0.0`
  - `vladimirovrosen/odoo-ee-mcp:1.0.0`
- External images pinned by digest (captured 2026-04-24):
  - `vladimirovrosen/odoo-claude-terminal@sha256:047e865131e1afd86eb…`
  - `ghcr.io/github/github-mcp-server@sha256:26db03408086a99cf1916348…`
- New overlay `docker-compose.prod-pins.yml` documents the production
  `image:` lines so any fresh stack deploy is reproducible.
- `:latest` tags remain as an emergency-downgrade pointer.

### Added — Phase 5: Prometheus `/metrics` scaffold
- New module `metrics.py` with Counters/Gauges:
  - `mcp_tool_calls_total{tool, status}` Counter
  - `mcp_proxy_discoveries_total{service, outcome}` Counter
  - `mcp_backup_writes_total{operation, tenant}` Counter
  - `mcp_active_sessions` Gauge
  - `mcp_http_requests_total{method, path_group, status}` Counter
  - `mcp_build_info{version}` Gauge (always 1, version label)
- Hooked at call sites: `call_tool`, `_discover_proxy_tools`,
  `_backup_write`. Gracefully no-ops when `prometheus-client` is not
  installed.
- Public `/metrics` endpoint — text/plain Prometheus format, no auth
  (convention). **Intended to be reachable from backend network only**
  — full scraping config + Grafana dashboards live in 3.x.
- New env `MCP_METRICS_ENABLED=1` (default) toggles emission.

### Dependencies (requirements.txt)
- Added: `boto3>=1.34.0`, `prometheus-client>=0.20.0`,
  `APScheduler>=3.10.0` (optional — scheduler is best-effort).

### Release — Phase 6
- `__version__` and `VERSION` bumped to 2.24.0.
- Docker: `vladimirovrosen/odoo-rpc-mcp:2.24.0` + `:latest` + `:stable`
  (re-points from 2.19.0).
- Memory/docs updated: `project_mcp_v2_24_0_plan.md`, MEMORY index entry.

### Known follow-ups (explicitly deferred to 3.x)
- Prometheus scraping config + Grafana dashboards + alerting.
- Portainer proxy tools absent from main's `tools/list` today — investigate.
- SSH bridge server deployment on poligroup (today only on odoo-dev-server).
- `ssh_execute` tool: `/home/mcp/.ssh/id_ed25519` perm denial; add
  SSH-agent-forward support so no bind-mounted key is required.
- Monaco editor (instead of plain textarea) in the filestore manager.
- Alerting integration (webhook/Slack/email) triggered from `/metrics`.

## [2.23.0] — 2026-04-24 — Stock initial-balance toolkit + backup plugin + feature flags

### Added — backup-mcp plugin
- New container `mcp-backup` (port 8092, backend network), cloned from `filesystem-mcp`
  pattern. Shared volume `mcp-backups` mounted rw at `/backups` in both `mcp-backup`
  (exposes list/read/delete via MCP filesystem protocol) and `mcp-odoo-rpc` (writes
  JSON snapshots before destructive stock ops). Future track: own UI + S3 Contabo
  sync + archive rotation.
- `proxy_services.json` entry `backup` → `http://backup-mcp:8092/sse`.
- Helper `_backup_write(operation, connection, payload)` in `server.py` — writes
  `/backups/<YYYY-MM-DD>/<op>_<HHMMSSfff>_<conn>.json`. Override path via
  `MCP_BACKUP_DIR` env var.

### Added — tz helpers (mandatory tz on all new datetime-sensitive tools)
- `_resolve_tz(tz_name)` — strict IANA validation via `zoneinfo`. Raises if missing.
- `_local_eod_to_utc(date, tz)` — converts caller-local 23:59:59 → UTC datetime
  for end-of-year opening-balance timestamps.
- Design rule: every new tool that writes dates to Odoo REQUIRES a `tz` parameter
  from the caller (the user's timezone) — no silent UTC fallback.

### Added — 3 stock initial-balance tools (v18 + v19 version-aware)
- `odoo_stock_initial_import` — SQL INSERT of opening stock balances, bypasses ORM
  overrides (e.g. custom `stock.move.create` that nulls `name`). Creates
  `stock.move` + `stock.move.line` + `stock.quant`; v14-v18 also inserts
  `stock.valuation.layer`; v19 stores value/price_unit/remaining_qty/remaining_value/
  is_valued/is_in directly on `stock.move` (SVL model doesn't exist). Refuses
  if any target `stock.quant` already has non-zero on-hand. Auto-resolves virtual
  inventory location (`usage=inventory`). Writes backup before INSERT.
- `odoo_stock_initial_delete` — cascade delete of wrong opening balances
  (`stock.move.is_inventory=TRUE`). v18: DELETE SVL → SML → SM + UPDATE quants.
  v19: DELETE SML → SM + UPDATE quants. Pre-flight guard: refuses if any affected
  SVL/stock.move has `account_move_id` set (would orphan journal entries). Full
  pre-delete snapshot written to `/backups` ALWAYS — even for dry_run previews.
- `odoo_stock_initial_opening_journal` — creates ONE `account.move` (MISC journal,
  posted) for the initial-balance value: DR per-category stock valuation accounts,
  CR contra (default `122000` Retained earnings). Duplicate guard scans for
  posted `account.move.line` on the target accounts + date before create
  (Alpinter lesson — comprehensive openings often already include stock lines).
  v18 sums `stock.valuation.layer.value`; v19 sums `stock.move.value` for
  `is_inventory=TRUE AND is_valued=TRUE`.

### Added — `MCP_DISABLE_FEATURES` env var (client-stack hardening)
- Comma-separated feature groups: `ssh, portainer, github, google, telegram,
  memory, ai, public, website, web, proxy`. Tools with matching prefixes are
  hidden from `list_tools` and blocked in `call_tool`. Proxy service names
  also accepted — they skip discovery entirely. Used on client stacks
  (mcp-115572378, -115353345, -203709674, -130931201, -208609891) to expose
  only core Odoo RPC + chosen plugins.
- `_tool_disabled()` predicate + filter in `list_tools`, `call_tool`, and
  `_discover_proxy_tools`.

## [2.19.0] — 2026-04-21 — AI OCR gap-closure set (Trust Foundation + 13 gaps)

### Trust Foundation (P0 — confidence-based auto-post)
- **Gap 1.1** — per-field confidence in vision prompt v2 (`_confidence` dict).
  `ExtractionResult.field_confidence` + `_extract_field_confidence` helper.
- **Gap 1.2** — arithmetic reconciliation (`_check_arithmetic`): sum(lines)
  vs amount_untaxed, untaxed+tax vs total, 2-cent tolerance.
- **Gap 3.1** — `ai_review_reason` rejection taxonomy Selection (9 codes).
- **Gap 3.2** — weighted per-field thresholds via `ai_field_thresholds_json`
  on res.company; hard gates that block auto-post independent of score.

### Circuit breakers & scaling (P0/P1)
- **Gap 4.2** — monthly budget cap: `res.company.ai_monthly_budget_eur` +
  `_step_guard_monthly_budget` (seq=15) + `ai_usage_log.monthly_cost_eur_mc`.
  New tool `ai_usage_budget_status`.
- **Gap 4.1** — attachment auto-trigger: `ir.attachment.create` hook + flag
  `ai_pipeline_requested` + `scan_pending(requested_only=True)`.
- **Gap 2.1** (Phase 1) — few-shot RAG: `_step_retrieve_few_shot_examples`
  (direct Odoo query on posted past bills, same partner, top 3) + injection
  in leading cached user message.
- **Gap 2.2** — partner→account coding memory:
  `_collect_partner_account_histogram` (last 30 posted lines) +
  `_format_partner_account_hints` inline with few-shot block.

### Bulgarian domain (P1)
- **Gap 1.3** — `bg_validators.py` (EIK/VAT/MRN regex + normalise), prompt
  v3 with `partner_eik`, `customs_mrn`, art.117 guidance. New pipeline step
  `normalize_bg_fields` (seq=150).
- **Gap 1.4** — prompt v4: explicit multi-page total guidance (last page,
  "Continued…" markers, rounding rows).

### Quality & cost (P1/P2)
- **Gap 1.6** — two-pass escalation: haiku→sonnet when critical fields
  confidence < 0.75. Opt-in via `res.company.ai_two_pass_escalation`.
- **Gap 1.7** — `count_pdf_pages` uses pypdf authoritative count (byte
  heuristic fallback). Fixes over-routing to sonnet on trivial PDFs.
- **Gap 4.3** — `pdf_sanitizer.py` strips /JS /OpenAction /AA /EmbeddedFiles
  before routing to vision API. Graceful fallback on malformed PDFs.
- **Gap 4.5** — friendly chatter table: `_render_extraction_chatter` with
  confidence badges, lines table, arithmetic status, collapsible raw JSON.
- **Gap 4.6** — Qdrant cross-company isolation guard in `ai.qdrant.client`.
- **Gap 4.7** — API key rotation: `claude_keys_rotated_at` + 90-day nag.

### Active learning (P1/P3)
- **Gap 3.3** — duplicate detection: `account.move._ai_check_duplicate`
  (same partner+ref OR same partner+date+total).
- **Gap 3.4** — `ai.correction` model (append-only) + `ai_extracted_snapshot`
  field + `write()` override captures field changes as training signal.
- **Gap 3.6** — ai.correction immutable write/unlink guards;
  `ai_usage_log.mark_billed(reason=...)` audit logging.
- **Gap 3.7** — reviewer dashboard: `severity` computed (high/medium/low/
  unknown) + graph + pivot views + "Prompt Tuning Queue" pre-filtered action.

### Infrastructure
- Dockerfile: +COPY `bg_validators.py` and `pdf_sanitizer.py`.
  `requirements.txt` +`pypdf>=4.0.0`. Maintainer updated.
- 156 pytest tests across 11 files covering all new code paths.

### Adjacent MCP fix
- **Odoo 19 MCP 403** — jsonrpc+api_key auto-fallback to xmlrpc on data ops
  (`effective_protocol` property + one-time warning log). Glue-side
  `claude_anthropic_api_key` field removal also landed in this cycle.

### Added — Context-aware translation tool (simple + HTML/XML)
- `odoo_translate_context_aware` — translate Odoo records using Claude with domain
  context for natural, fluent results (not literal). **Auto-detects field kind** and
  handles both paths in one call:
  - **Simple char/text** (e.g. ir.ui.menu.name, account.account.name): batch translate
    via `update_field_translations`. Context: Odoo model, parent chain (for menus:
    "Sales > Orders > Quotations"), existing translations, user domain hint.
  - **HTML/XML** (e.g. ir.ui.view.arch_db, website.page, product.template.website_description):
    extracts canonical terms via `get_field_translations`, translates each term preserving
    inline HTML tags (<strong>, <a>, <span>...), writes back in terms mode. Per-record,
    per-field Claude call (HTML payloads are large).
  Validates target language is active, fields are translatable, Odoo ≥ 16.
  Uses `_ai_tenant_credentials()` for ANTHROPIC_API_KEY resolution (per-tenant override
  supported). Recommended models: haiku-4-5 (menu labels), sonnet-4-6 (balanced, website
  pages), opus-4-7 (complex terminology). Supports `dry_run=True`.

### Added — 4 stock operation tools (BG workflows, v14-v19 compatible)
- `odoo_stock_mo_delete_draft` — safely DELETE a draft/cancelled mrp.production with
  cascade (raw + finished stock.moves, procurement.group if orphaned). Bypasses Odoo's
  "cannot be deleted" constraint via raw SQL + ir.actions.server. Refuses if MO has
  any SVL / valued stock.move / qty_produced > 0. Checks for account.move records
  matching name (warns, doesn't delete). Version-aware SVL lookup.
- `odoo_record_backup` — utility: reads full field snapshot of any records (excluding
  binary fields) + optional related-record queries. Returns structured JSON. Use
  BEFORE destructive ops to capture state for rollback. Does NOT write to disk —
  caller decides where to persist (recommended: `~/.claude/clients/<conn>/backup_<op>_<date>.json`).

### Added — 2 stock operation tools (BG workflows, v14-v19 compatible)
- `odoo_stock_product_flip_to_storable` — flip a product from consu (is_storable=false)
  to storable when it already has stock.move records. Bypasses Odoo's ORM constraint
  via raw SQL in an ir.actions.server (atomic flip + quant INSERT in one transaction),
  so no duplicate SVL/valuation is created. Supports `dry_run=True` (default) with
  preview, warnings for edge cases (already storable, existing quant, lot tracking),
  location↔company validation, ISO datetime parsing for `in_date`.
  Works on Odoo 18 and 19 (`is_storable` + `stock.quant` are identical across versions).
- `odoo_stock_close_unaccounted_value` — create an Inventory Valuation journal entry
  (Dr stock valuation / Cr GRNI) for a stocked record that has `account_move_id=false`,
  then bind the new account.move back. **Version-aware**: auto-detects whether to
  operate on `stock.valuation.layer` (v14-18) or `stock.move` (v19+, since SVL was
  merged into stock.move). GRNI account auto-detect order: (a) v14-18
  `property_stock_account_input_categ_id`, (b) v19 + `l10n_bg_stock_account`
  `l10n_bg_stock_input_account_id`, (c) v19 vanilla fallback `account_stock_variation_id`.
  User can override via `grni_account_id`. Validates journal type='general' and
  company match. Supports `dry_run=True`.

Both tools read accounts from `product.category` properties — no hardcoded account IDs.
Recipe derived from 2026-04-21 Alpinter Bulgaria prod session.

## [2.10.0] — 2026-04-19

### Added — 4 translate tools (multi-language field writes/reads)
- `odoo_list_translatable_fields(model)` — discovers which fields on a
  model are translatable; classifies each as `simple` (translate=True),
  `html` (html_translate), `xml` (xml_translate), `callable` (other
  truthy), or `none`. Field type + name heuristic compensates for
  XML-RPC flattening callable values to `True`.
- `odoo_get_field_translations(model, res_id, field_name)` — reads
  current per-lang translations. Auto-detects kind and uses the right
  API surface (`get_field_translations` on 16+, `ir.translation`
  fallback pre-16).
- `odoo_translate_field` — writes translations for simple
  `translate=True` fields. Validates field + lang activation + refuses
  HTML/XML kinds with an actionable error pointing to the right tool.
  Version-oriented: 16+ native, <16 ir.translation fallback.
- `odoo_translate_html` — writes translations for `html_translate` /
  `xml_translate` fields. Three modes:
  - `extract` — read-only; returns canonical terms as Odoo's engine
    sees them (HTML blocks preserving inline tags).
  - `terms` — direct `{lang: {src_term: tr_term}}` map.
  - `replace` — `{lang: full_html_string}`; delegated to Odoo ORM via
    `write(..., context={'lang': lg})` so the native
    `html_translate`/`xml_translate` engine aligns terms — same path
    the Website editor uses.

### Added — 5 website snippet tools (widgets + banners)
- `odoo_website_list_snippets` — list available Odoo snippet templates
  (ir.ui.view with key containing `.s_`). Categorises: structure /
  content / dynamic / effect / unknown. Filters by category, module,
  search keyword.
- `odoo_website_list_page_snippets(target)` — lxml-parses a target HTML
  field (blog.post.content, ir.ui.view.arch_db, product.template.
  website_description, etc.), returns all snippets with index, xpath,
  data-name, text preview, background URL. Detection via both
  `data-snippet` attr AND first `s_*` class (Odoo strips data-snippet
  on some saves).
- `odoo_website_add_snippet` — fetches snippet arch from
  `ir.ui.view`, extracts root element (skips `<template>` / `<t>`
  wrappers), applies optional pre-insertion substitutions, inserts at
  position (`end`, `begin`, `after`, `before`, `replace`) relative to
  optional anchor_xpath.
- `odoo_website_update_snippet` — locates snippet by xpath, applies
  substitutions. Syntax:
  - `{'.//h2': 'Title'}` — text replace
  - `{'.//img/@src': 'url'}` — attribute set
  - `{'./div/@style:background-image': 'url(...)'}` — CSS property
    (preserves other style props)
- `odoo_website_remove_snippet` — removes snippet at xpath.

### Fixed — auto-ZWSP to mark identical translations as translated
- Odoo's `update_field_translations` silently drops `(lang, term)`
  entries where value == source; the website translation editor then
  flags those as "untranslated" even when the translator intentionally
  kept them identical (URLs, brand names, code refs).
- Both `odoo_translate_field` and `odoo_translate_html` now
  transparently prefix identical values with U+200B (zero-width space),
  so Odoo keeps them as explicit "translated, kept identical" entries.
  Opt-out via `mark_identical_as_translated=false`.
- Response includes `zwsp_filled_identical: {lang: count}`.

### Fixed — earlier translate tool regressions
- `_field_translate_kind` now uses field type + name heuristic as
  fallback because XML-RPC flattens callable translate values to True.
- `odoo_translate_html(mode='extract')` now correctly parses the flat
  per-term-per-lang list structure of `get_field_translations()` for
  html_translate fields (was assuming nested dict).
- `odoo_translate_html(mode='replace')` rewritten to delegate to
  Odoo's native engine via `write()` + lang context (previous stdlib
  HTMLParser approach mismatched term counts for nested HTML).

### Added — lxml dependency
- `requirements.txt`: `lxml>=5.2.0` for snippet HTML parsing/mutation.

### Verified
- E2E test against BL Consulting blog.post id=180 (Odoo 19.0+e):
  51 terms extracted, BG translations intact, banner image swap +
  CTA card add/remove round-trip clean.

## [2.9.x] — 2026-04-18 (intermediate rebuilds)

Development iterations during 2.10 feature work. Use 2.10.0 for
production.

## [2.8.0] — 2026-04-18

### Added — verify_ssl + cert pinning (TOFU)
- Per-connection `verify_ssl` flag on OdooConnection. When disabled,
  the first HTTPS call fetches + pins the peer cert under
  `/data/ssl_certs/<alias>.pem`; subsequent calls verify against the
  pinned cert (trust-on-first-use). New tools: `odoo_cert_info`,
  `odoo_cert_refresh`.
- `MCP_ADMIN_TOKEN` env + `/admin/memory/{upload,remove,list}`
  endpoints for memory pack management.

## [2.7.0] — 2026-04-18

### Added — Licensed memory scope
- Per-tenant memory storage with `memory_share` scope `licensed`.

### Fixed — Cloudflare Bot Fight Mode false positives
- `odoo-rpc-mcp/server.py:_xmlrpc_validate` used the default
  `xmlrpc.client.ServerProxy` transport, which sends
  `User-Agent: Python-xmlrpc/3.x`. Cloudflare Bot Fight Mode blocks
  this UA on Free-tier zones, so Odoo instances behind CF returned
  `authenticate() == False` even with a valid key. Added
  `_UATransport` / `_UASafeTransport` subclasses that send
  `OdooMcpAuth/1.0 (+https://mcp.odoo-shell.space)` instead.
  HTTP/HTTPS split is explicit — `SafeTransport` only for `https://`.
- `claude-terminal/start-session.sh` register-connection request now
  sends `User-Agent: ClaudeTerminalStartSession/1.0`. Without it, the
  register call fails with 403 when MCP is fronted by Cloudflare
  (identified during 13-connection batch test — all POSTs returned
  Cloudflare 403 before any logic ran).

### Added — Integration test suite + plan completion (tasks 8–10)
- `odoo-rpc-mcp/tests/test_unified_auth.sh` — 10 scenario bash test:
  register negative/positive/conflict, identify stdio-compat vs
  unified-auth spoof defense, cache hit latency, whitelist
  enforcement, full register→identify cycle. 9/10 passing, 1 skipped
  when `ALLOWED_ODOO_URLS` is empty (whitelist verified manually
  with `ALLOWED_ODOO_URLS=https://ussmed.odoo.com` → non-whitelisted
  URL → 401).
- **Task 8** (stdio backwards compat): ToDo-state already satisfied
  in tasks 2/3/4 — when no HTTP auth context exists (no
  `X-Odoo-Url` header), `_get_current_user()` falls back to
  `identify()`-set session slot. Test T5 asserts this.
- **Task 9** (whitelist enforcement): `ALLOWED_ODOO_URLS` env wired
  into `docker-compose.yml` for the `odoo-rpc-mcp` service.
  Non-whitelisted URL with valid key → 401 (test T9).
- **Task 10** (integration tests): the full suite above.

### Added — Web login for terminal gateway (task 7 от unified auth plan)
- `claude-terminal/landing.html` вече съдържа login форма (Display name,
  Odoo URL, Database, Login, Alias, API Key). При submit прави
  `POST /api/user/register-connection`. Успех → redirect към terminal
  с URL args, които start-session.sh обработва.
- Non-secret полета се cache-ват в `localStorage` (`mcp_web_login_v1`),
  API ключът винаги се въвежда ръчно за да не виси в browser storage.
- `gateway.js` добавя whitelist proxy за избрани MCP endpoints
  (`/api/user/register-connection`, `/health`) за да може landing-ът
  да се обръща към MCP без CORS. `X-Forwarded-For` се пренася.
- Env vars `MCP_HOST` (default `odoo-rpc-mcp`) и `MCP_PORT` (default
  `8084`) конфигурират upstream-а на proxy-то.

### Changed — External terminal flow (task 5 от unified auth plan)
- `claude-terminal/start-session.sh` вече регистрира Odoo връзката в MCP
  през новия `POST /api/user/register-connection` endpoint (бивш
  `/api/identify` call е премахнат — unified-auth headers правят
  identify-а автоматичен при първата tool call).
- `.mcp.json` за всяка терминална сесия се генерира динамично с
  `Authorization: Bearer`, `X-Odoo-Url`, `X-Odoo-Db`, `X-Odoo-Login`
  заглавки за `odoo-rpc` MCP service. Всеки tool call от Claude CLI
  носи валидиращата се 4-ка, middleware-ът я resolve-ва към profile.
- JSON payload-и за register и `.mcp.json` се build-ват през Python
  (`json.dumps`), не през bash heredoc — избягва escaping проблеми
  при UTF-8 имена, кавички, интервали.
- Reference `claude-terminal/.mcp.json` обновен с `<...>` placeholders
  за документация.

### Security — Lock memory / user_connection to validated user (task 4)
- `_get_current_user()` docstring enforces invariant: identity never
  reads from `args`, only from ContextVar or per-session identify state.
- `identify` tool no longer writes to `_session_users` when a
  validated caller is present — ContextVar is authoritative; stale
  session state would only confuse later non-HTTP tool calls.
- `memory_*` and `user_connection_*` tools were already passing
  identity through `_get_current_user`, so they are now transparently
  locked to the validated caller without additional per-tool changes.

### Security — `identify()` refactor (task 3 от unified auth plan)
- MCP tool `identify` и HTTP `POST /api/identify` вече използват валидирания
  caller от `_odoo_caller_ctx` (HTTP middleware). `args["name"]` / `body.name`
  се чете само като fallback за stdio/dev (когато няма HTTP auth context).
  При валидна unified-auth сесия име от клиента се **игнорира** — profile
  spoofing през `identify(name="somebody_else")` вече не е възможен.
- Response съдържа ново поле `validated: bool` — true когато identity
  идва от XMLRPC-валидиран key, false в legacy mode.
- При unified-auth се авто-активира alias-ът, който caller-ът е използвал
  (не само последно-записаният `active_connection`).

### Added — Unified Auth middleware (task 2 от MCP unified auth plan)
- **`get_caller_odoo_user(headers)`** middleware: валидира `Authorization:
  Bearer <api_key>` + `X-Odoo-Url` + `X-Odoo-Db` + `X-Odoo-Login` срещу
  Odoo XMLRPC `common.authenticate(db, login, api_key, {})` → uid. Cache
  5 мин (TTL през env `AUTH_CACHE_TTL`).
- **`_resolve_mcp_user(url, db, login, api_key)`** — сканира
  `data/users/*/connections.json` и връща MCP user profile който съдържа
  точно тази 4-ка. Идентичността се определя от регистрираните
  connections, не от arbitrary client claim.
- **ContextVar `_odoo_caller_ctx`** — per-async-task validated caller,
  set от ASGI middleware-а, четен от `_get_current_user()` с приоритет
  над per-session identify().
- **Нов endpoint `POST /api/user/register-connection`** — self-register
  (alias → url/db/login/api_key) под MCP profile. Auth-ът е built-in:
  XMLRPC validate на body-то. Ownership proof: ако profile вече съдържа
  connections, новата трябва да дели (url, db, login) с поне една
  съществуваща — иначе 403. Conflict (същата 4-ка в друг profile) → 409.
- **Whitelist `ALLOWED_ODOO_URLS`** (env) — preview за task 9.

### Security
- **Fix `existing_profiles` information leak in `identify()`** — премахнато изложено поле
  `existing_profiles` (и от MCP tool, и от HTTP `/api/identify`) което връщаше
  списък с ВСИЧКИ съществуващи potребителски profiles на каещия се caller.
  Това позволяваше enumeration на чужди профили. `is_new` вече се изчислява
  директно през `os.path.isdir` без листване. Hint за нов profile също не
  изброява съществуващи. Спойка за task 1 от unified-auth плана.

## [2.4.1] - 2026-04-15

### Added — Kubernetes deployment (k3s / Rancher)
Нова папка `k3s/` с Kustomize манифести за deploy на целия стак върху k3s
клъстер управляван от Rancher.

- `k3s/base/` — всички ресурси (10 Deployments, 10 Services, 5 PVC-та, 2 Traefik
  IngressRoute-а, ConfigMaps + Secret template). Namespace `odoo-mcp`.
  Мрежовата сегментация public/backend от docker-compose се пази през label
  `tier` + Ingress само за двата public workload-а (claude-terminal, odoo-rpc-mcp).
- `k3s/overlays/prod/` — deploy с Ingress + TLS (за Cloudflare Tunnel или
  certResolver). secretGenerator от `.env`, configMapGenerator за
  `proxy_services.json` и claude-terminal templates. Images override-ване.
- `k3s/overlays/direct/` — deploy БЕЗ Cloudflare. Експозиция през NodePort
  (30080 за claude-terminal, 30084 за odoo-rpc-mcp), Ingress патчнат на
  plain HTTP. Включва `cert-manager-example.yaml` за Let's Encrypt HTTP-01
  challenge. Алтернатива: k3s Klipper LoadBalancer на портове 80/443.
- `k3s/README.md` — deployment guide с два варианта (kubectl / Rancher UI),
  Rancher-специфични бележки (project binding, Monitoring/Logging/Backup/RBAC,
  Fleet GitOps), TODO list.

### Added — Docker Compose: Qdrant + Ollama
Добавени са двете backend услуги за AI Tokenizer стак-а (companion на
`ai_tokenizer` модул-а в `l10n_bg_claude_terminal`):

- `qdrant` (REST 6333, gRPC 6334, volume `qdrant-storage`)
- `ollama` (port 11434, volume `ollama-data`) — pull-ва `nomic-embed-text`

### Changed
- `claude-terminal/CLAUDE.md` — startup sequence на български с 4 стъпки:
  `~/.odoo_session.json` → `identify()` → `memory_pull('*')` →
  `user_connection_list()`. Добавени правила за multi-user изолация.

## [2.4.0] - 2026-04-15

### Added — AI Tokenizer tools (5 new MCP tools)
Companion to `l10n_bg_claude_terminal` v18.0.1.23.0 / v19.0.1.18.0.
All tools delegate to Odoo (which talks to Qdrant + Ollama / OpenAI / Voyage).

- `ai_tokenize_record(model, id, view_type='form')` — synchronous tokenize-and-index
  of a single record. Returns `{ok, document_id, state, token_count, error}`.
  Calls `ai.view.registry.tokenize_record()`.
- `ai_tokenize_collection(model, view_type='form')` — bulk tokenize all records
  of a model. Auto-creates the registry entry if missing, ensures it's active,
  returns indexed count.
- `ai_search_similar(query, model='', view_type='', company_id=0, limit=10,
  score_threshold=0.0)` — semantic search via Qdrant. Embeds the query with
  the configured provider, returns ranked hits with `model`, `res_id`,
  `display_name`, `score`, `snippet`, `view_type`, `qdrant_point_id`.
  Filters: model/view_type/company_id; `db_name` is auto-applied for
  multi-DB Qdrant isolation.
- `ai_list_documents(model='', state='', limit=50)` — list `ai.composite.document`
  rows; useful for monitoring / debugging which records are indexed, stale,
  or in error.
- `ai_collection_info()` — returns Qdrant collection stats: vector size,
  distance, points count, plus Odoo-side indexed-document count for cross-check.

## [2.3.0] - 2026-04-08

### Added
- **OCA MCP plugin** (14 tools): OCA maintainer-tools wrapper for addon repo management
  - `oca_clone_all`, `oca_clone_repo`, `oca_update`, `oca_status`, `oca_search`
  - `oca_deploy` (buffered mode), `oca_link` (symlink to addons_path)
  - `oca_gen_readme`, `oca_gen_table`, `oca_gen_icon`, `oca_gen_requirements`
  - `oca_changelog`, `oca_migrate`, `oca_fix_website`
  - Dual mode: direct (/opt/odoo) or buffered (/repos/{instance})
  - Docker image: `vladimirovrosen/odoo-oca-mcp:latest`
- **EE MCP plugin** (12 tools): Odoo Enterprise module management
  - `ee_clone`, `ee_update`, `ee_modules`, `ee_search`, `ee_link`, `ee_unlink`
  - `ee_depends` (full dependency tree CE+EE), `ee_deploy`
  - `ee_token_check` (validate GitHub access to odoo/enterprise)
  - `ee_license_status` (read expiration from Odoo instance)
  - `ee_oca_conflicts` (name collision + model overlap detection)
  - `ee_oca_recommend` (compare and recommend EE vs OCA version)
  - Docker image: `vladimirovrosen/odoo-ee-mcp:latest`
- **Web Session tools** (7): Cookie-based HTTP access to Odoo web controllers
  - `odoo_web_login` — authenticate with user/password, persistent session
  - `odoo_web_call` — JSON-RPC call_kw via web session
  - `odoo_web_read` — web_search_read (frontend format)
  - `odoo_web_export` — export_data via web session
  - `odoo_web_report` — download PDF report via web session
  - `odoo_web_request` — raw HTTP request to any controller URL
  - `odoo_web_logout` — destroy session
  - Auto-reads credentials from connection config (web.login/password)
  - CSRF token auto-extraction for HTTP controller routes
- **Public Access tools** (15): Direct controller route access via web session
  - Export: `public_access_export_xlsx`, `public_access_export_csv` (with CSRF)
  - Reports: `public_access_report_pdf`, `public_access_report_html`, `public_access_report_xlsx`
  - Downloads: `public_access_download`, `public_access_image`, `public_access_barcode`
  - Portal: `public_access_portal_home/invoices/orders/purchases/tickets`
  - Website: `public_access_shop`, `public_access_sitemap`
- **`odoo_module_info`**: Cross-reference module RPC state + filesystem locations (OCA/EE/custom)
- **`odoo_attachment_download`**: Download ir.attachment by ID (base64 or save to disk)
- **Web session config**: `web.login/password` section in connections.json
- **GUI**: Web Session expander in GTK4 Connection Manager (login, password, test button)

### Security
- **Per-session user isolation**: `identify()` uses `id(ServerSession)` to isolate concurrent users
  - Each MCP client (claude.ai, PyCharm, terminal) gets unique session key
  - Prevents cross-user data leakage on shared public server
- **Cyrillic transliteration**: User names converted to Latin for directory names

### Changed
- Total tools: 188 (83 native + 105 proxied)
- Proxied breakdown: portainer 39 + github 26 + filesystem 14 + oca 14 + ee 12
- `user_connection_add` supports `web_login`/`web_password` parameters
- `_get_current_user()` uses MCP request_context for per-session resolution

### Docker Hub Images (7)
- `vladimirovrosen/odoo-rpc-mcp:latest`
- `vladimirovrosen/odoo-claude-terminal:latest`
- `vladimirovrosen/odoo-filesystem-mcp:latest`
- `vladimirovrosen/odoo-github-mcp:latest`
- `vladimirovrosen/odoo-portainer-mcp:latest`
- `vladimirovrosen/odoo-oca-mcp:latest` (NEW)
- `vladimirovrosen/odoo-ee-mcp:latest` (NEW)

## [2.1.0] - 2026-04-08

### Added
- **Filesystem MCP plugin**: Isolated file/folder management service
  - Official `@modelcontextprotocol/server-filesystem` + supergateway (SSE)
  - 14 tools: read_file, write_file, edit_file, create_directory, list_directory, move_file, delete_file, search_files, get_file_info, list_allowed_directories, read_multiple_files, read_media_file, search_within_files, tree
  - Sandboxed in `/repos` volume — all operations restricted to allowed directories
  - Docker image: `vladimirovrosen/odoo-filesystem-mcp:latest`
- **GitHub MCP plugin** (rebuilt): Switched from HTTP to SSE transport
  - Official `@modelcontextprotocol/server-github` + supergateway
  - 26 tools: repos, issues, PRs, code search, file operations, branches
  - Docker image: `vladimirovrosen/odoo-github-mcp:latest`
- **Terminal theme support**: Per-user color themes via URL parameter
  - 19 themes (9 light + 10 dark): github, dracula, monokai, solarized, gruvbox, atom, etc.
  - `themes.json` — shared theme definitions
  - OSC escape sequences applied per-session in `start-session.sh`
  - Odoo module: `claude_theme` Selection field in user preferences
  - URL parameter: `&arg=CLAUDE_THEME=dracula`
- **`/api/identify` REST endpoint**: Terminal auto-identifies with MCP server on login
  - Returns profile name, data directories, existing profiles list
  - Creates symlinks in terminal HOME to shared MCP data (`mcp-data`, `mcp-memory`)
- **`odoo_attachment_download` tool**: Download `ir.attachment` by ID
  - Returns base64 content, filename, mimetype, size
  - Optional `save_path` to save file directly to disk
- **Cyrillic transliteration**: User names properly converted to Latin for directory names
  - `Росен` → `rosen`, `Иван Петров` → `ivan_petrov`
  - Supports Bulgarian, Ukrainian, Russian Cyrillic + accented Latin (NFKD)
- **Lazy directory creation**: User/memory directories created only on write, not on read
  - `identify()` no longer creates empty directories
  - Returns `existing_profiles` list and `new_profile` status for unknown users

### Changed
- Total tools on single endpoint: 139 (60 native + 79 proxied)
- Proxied breakdown: portainer 39 + github 26 + filesystem 14
- `entrypoint.sh`: reads theme from `themes.json` instead of bash associative array
- `docker-compose.yml`: added `filesystem-mcp` service, `mcp-repos` volume, fixed `CLAUDE_THEME` default
- `Dockerfile`: added `themes.json`, `gateway.js`, `landing.html` to image build

### Fixed
- `portainer-mcp` compatibility with Portainer 2.33.x (`-disable-version-check` flag)
- `proxy_services.json` Docker mount conflict (directory vs file)
- Odoo module: defensive `getattr()` for `claude_theme` field (prevents crash before module upgrade)

### Docker Hub Images
- `vladimirovrosen/odoo-rpc-mcp:latest`
- `vladimirovrosen/odoo-claude-terminal:latest`
- `vladimirovrosen/odoo-filesystem-mcp:latest` (NEW)
- `vladimirovrosen/odoo-github-mcp:latest` (NEW)
- `vladimirovrosen/odoo-portainer-mcp:latest`

## [2.0.0] - 2026-04-07

### Added
- **Proxy gateway architecture**: odoo-rpc-mcp acts as single public endpoint, proxying to backend services
  - Dynamic tool discovery at startup — sub-service tools registered with prefix (`portainer__listStacks`, `github__get_me`)
  - `proxy_call` — manual proxy forwarding to any backend service
  - `proxy_discover` — list tools on a specific backend service
  - `proxy_refresh` — re-discover tools after adding/restarting services
  - SSE backends proxied via subprocess for supergateway compatibility
  - HTTP backends proxied via async MCP client
- **Plugin architecture**: `proxy_services.json` config file for adding new MCP backends
  - No code changes needed — edit JSON, restart, refresh
  - Headers support `${ENV_VAR}` expansion
  - Also configurable via `PROXY_SERVICES_JSON` env var
- **Dual-network Docker architecture**: `public` + `backend` networks
  - Backend services (portainer, github, teams) have NO host port mappings
  - Only odoo-rpc-mcp (8084) and claude-terminal (8080) are publicly accessible
  - Services communicate by hostname on internal Docker network
- **Microsoft Teams MCP**: InditexTech server with supergateway wrapper
  - 6 tools: start_thread, update_thread, read_thread, list_threads, get_member_by_name, list_members
  - Azure AD OAuth 2.0 authentication
  - Custom Dockerfile with supergateway on port 8087

### Changed
- Total tools on single endpoint: 116 (60 native + 39 portainer + 20 github - 3 proxy meta)
- Architecture: from 4 independent services to gateway + backend plugins
- README fully rewritten for proxy gateway architecture

## [1.4.0] - 2026-04-07

### Added
- **Memory storage system**: Shared and per-user memory file storage via MCP tools
  - `memory_list` — List personal and/or shared memory files with metadata
  - `memory_read` — Read a memory file (searches personal first, then shared)
  - `memory_write` — Save/update memory files to personal or shared storage
  - `memory_delete` — Delete memory files
  - `memory_share` — Copy personal memory to shared storage for colleagues
  - `memory_pull` — Pull shared memory into personal storage
- Storage structure: `/data/memory/shared/` (team) + `/data/memory/users/{name}/` (personal)
- Frontmatter parsing for file descriptions and types in `memory_list`

### Changed
- Total MCP tools in odoo-rpc-mcp: 49 → 55
- Total tools across all services: 107 → 113

## [1.3.0] - 2026-04-07

### Added
- **Per-user identity system**: `identify`, `who_am_i` — each Claude session identifies its user
- **Per-user connections**: `user_connection_add`, `user_connection_list`, `user_connection_activate`, `user_connection_delete` — personal connection storage per user
- **OAuth 2.0 authentication** for cloud-hosted MCP (claude.ai remote connectors)
- **API token authentication** for public-facing deployments (`X-Api-Token` / `?token=`)
- **SSH agent forwarding** for `git_remote` and `ssh_execute` tools
- **Landing page** with Odoo-style design, cover image, setup guide, and glassmorphism UI

### Changed
- Total MCP tools in odoo-rpc-mcp: 38 → 49
- Total tools across all services: 96 → 107
- Docker images published to Docker Hub: `vladimirovrosen/odoo-rpc-mcp`, `vladimirovrosen/odoo-portainer-mcp`, `vladimirovrosen/odoo-claude-terminal`
- README fully rewritten with complete tool reference and authentication docs

## [1.2.0] - 2026-04-04

### Added
- **Telegram integration**: Personal account messaging via Telethon client API
  - `telegram_configure` — Set API credentials (api_id + api_hash from my.telegram.org)
  - `telegram_auth` — Two-step phone + code authentication, 2FA support
  - `telegram_auth_status` — Check authentication status
  - `telegram_get_dialogs` — List recent chats (users, groups, channels)
  - `telegram_search_contacts` — Search contacts by name/username
  - `telegram_get_messages` — Read messages from any chat with text search
  - `telegram_send_message` — Send messages and replies
- New file `telegram_service.py` — TelegramServiceManager with session persistence
- Telethon dependency added to requirements.txt
- Docker environment variables: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH`

### Changed
- Total MCP tools: 30 → 38

## [1.1.0] - 2026-04-04

### Added
- **Google Gmail integration**: OAuth2 authentication, search, read, send/reply emails, list labels
  - `google_auth` — OAuth2 flow with saved tokens (credentials.json from Google Cloud Console)
  - `google_auth_status` — Check authentication status
  - `google_gmail_search` — Full Gmail search syntax support
  - `google_gmail_read` — Read message with full body extraction (plain text + HTML, nested multipart)
  - `google_gmail_send` — Send new emails or reply to existing threads
  - `google_gmail_labels` — List all Gmail labels/folders
- **Google Calendar integration**: List calendars, CRUD events, timezone support
  - `google_calendar_list` — List available calendars
  - `google_calendar_events` — List upcoming events with time range and text search
  - `google_calendar_create_event` — Create events with attendees, location, description
  - `google_calendar_update_event` — Partial update of existing events
  - `google_calendar_delete_event` — Delete events
- New file `google_service.py` — GoogleServiceManager class with OAuth2 token persistence
- Google API dependencies added to requirements.txt
- Docker environment variables: `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`

### Changed
- Server now initializes GoogleServiceManager at startup (auto-loads saved token)
- Dockerfile updated to include `google_service.py`
- Total MCP tools: 19 → 30

## [1.0.0] - 2026-03-30

### Added
- Initial release with 19 Odoo RPC tools
- Connection management: `odoo_connect`, `odoo_disconnect`, `odoo_connections`
- Introspection: `odoo_version`, `odoo_list_models`, `odoo_fields_get`
- CRUD: `odoo_search`, `odoo_read`, `odoo_search_read`, `odoo_search_count`, `odoo_create`, `odoo_write`, `odoo_unlink`
- Advanced: `odoo_execute`, `odoo_report`
- View refresh: `odoo_refresh` (push reload to browser via l10n_bg_claude_terminal)
- Fiscal position configuration (Bulgarian localization): `odoo_fp_list`, `odoo_fp_details`, `odoo_fp_configure`, `odoo_fp_remove_action`, `odoo_fp_types`
- Multi-connection support with named aliases
- XML-RPC (Odoo 8+) and JSON-RPC (Odoo 14+) protocols
- Streamable HTTP + SSE transport
- Docker deployment with claude-terminal and odoo-rpc-mcp services
- Standalone tools: odoo_connect.py (GTK4 GUI), odoo_module_analyzer.py, glb_viewer.py
