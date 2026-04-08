# Changelog

All notable changes to the Odoo RPC MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
