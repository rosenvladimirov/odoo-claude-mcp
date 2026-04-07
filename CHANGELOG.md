# Changelog

All notable changes to the Odoo RPC MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
