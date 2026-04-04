# Changelog

All notable changes to the Odoo RPC MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
