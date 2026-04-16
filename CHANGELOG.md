# Changelog

All notable changes to the Odoo RPC MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
