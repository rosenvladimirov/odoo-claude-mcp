# odoo-claude-mcp

Docker-based MCP server stack for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — integrates **Odoo ERP**, **Docker/Portainer**, **GitHub**, **Microsoft Teams**, **SSH**, **Gmail**, **Google Calendar**, and **Telegram** into a unified AI workflow via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

> **116 MCP tools** on a single endpoint — plugin architecture, dual-network isolation, proxy gateway.

## Architecture

```
              Internet / Cloudflare
                     │
         ┌───────────┼───────────┐
         │     public network    │
         │                       │
    ┌────▼─────┐          ┌──────▼──────┐
    │ odoo-rpc │          │   claude-   │
    │   :8084  │          │  terminal   │
    │ 60 native│          │   :8080     │
    │ +59 proxy│          │             │
    │ =116 tot │          │             │
    └────┬─────┘          └──────┬──────┘
         │     backend network   │
    ┌────▼─────┐ ┌────────┐ ┌───▼────┐
    │portainer │ │ github │ │ teams  │
    │  :8085   │ │ :8086  │ │ :8087  │
    │ 39 tools │ │ 20 tools│ │ 6 tools│
    └──────────┘ └────────┘ └────────┘
       No ports exposed — internal only
       Accessed by hostname via proxy
```

**Key design:**
- `odoo-rpc-mcp` is the **single public gateway** (port 8084) — all traffic goes through it
- Backend services (portainer, github, teams) have **no host port mappings** — completely isolated
- `odoo-rpc-mcp` bridges both networks and **proxies** backend tools with prefix naming (`portainer__listStacks`, `github__get_me`)
- At startup, the gateway **auto-discovers** tools from all sub-services and registers them as native tools
- **Plugin architecture** — add new MCP services via `proxy_services.json` without code changes

## Key Features

- **Full Odoo CRUD** — search, read, create, write, delete records in any Odoo model (v8–19+)
- **Multi-protocol** — XML-RPC (Odoo 8+) and JSON-RPC (Odoo 14+)
- **Multi-connection** — manage multiple Odoo instances, switch between them on the fly
- **Per-user identity** — each Claude session identifies its user, loads personal connections
- **Fiscal positions** — list, inspect, configure, and manage Bulgarian tax fiscal positions
- **Gmail & Calendar** — OAuth2 integration: search/read/send emails, manage calendar events
- **Telegram** — search contacts, read/send messages via personal Telegram account
- **SSH remote** — execute commands on remote servers, run git operations over SSH
- **GitHub API** — direct REST API + proxied 20 GitHub MCP tools
- **Docker management** — 39 Portainer tools for containers, stacks, environments, K8s
- **Microsoft Teams** — 6 tools for channel threads and member management
- **Memory storage** — shared knowledge base with per-user and team-wide memory files
- **Plugin system** — add new MCP backends via JSON config, no code changes
- **Connection Manager GUI** — desktop app (GTK4 on Linux, Qt6 on Windows/macOS)
- **OAuth 2.0 & API tokens** — secure access for cloud-hosted (claude.ai) and local deployments

## Services

| Service | Port | Network | Transport | Tools | Description |
|---------|------|---------|-----------|-------|-------------|
| `odoo-rpc-mcp` | 8084 | public + backend | HTTP | 60 native + 59 proxied | Gateway: Odoo + Gmail + Calendar + Telegram + SSH + Git + Memory + Proxy |
| `portainer-mcp` | 8085 | backend only | SSE | 39 | Docker/K8s management via Portainer |
| `github-mcp` | 8086 | backend only | HTTP | 20 | GitHub repo management (official server) |
| `teams-mcp` | 8087 | backend only | SSE | 6 | Microsoft Teams messaging (InditexTech) |
| `claude-terminal` | 8080 | public + backend | — | — | Web terminal (ttyd + Claude Code CLI) |

**Total: 116 MCP tools on one endpoint**

## Plugin Architecture

Add new MCP backends without changing code — edit `proxy_services.json`:

```json
{
  "portainer": {
    "transport": "sse",
    "url": "http://portainer-mcp:8085/sse"
  },
  "github": {
    "transport": "http",
    "url": "http://github-mcp:8086/mcp",
    "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}
  },
  "teams": {
    "transport": "sse",
    "url": "http://teams-mcp:8087/sse"
  },
  "my-new-service": {
    "transport": "sse",
    "url": "http://my-service:9000/sse"
  }
}
```

**Adding a new plugin:**
1. Add the service to `docker-compose.yml` on the `backend` network
2. Add its entry to `proxy_services.json`
3. `docker compose up -d` + call `proxy_refresh` — tools appear automatically

Headers support `${ENV_VAR}` expansion. Config is also available via `PROXY_SERVICES_JSON` env var.

## Quick Start

### Linux / macOS

```bash
curl -fsSL https://raw.githubusercontent.com/rosenvladimirov/odoo-claude-mcp/main/install.sh | bash
```

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

### Manual

```bash
git clone https://github.com/rosenvladimirov/odoo-claude-mcp.git
cd odoo-claude-mcp
cp .env.example .env    # edit: set ANTHROPIC_API_KEY
cp proxy_services.json.example proxy_services.json
docker compose up -d --build
```

### Register MCP with Claude Code

Only one endpoint needed — the gateway handles everything:

```bash
claude mcp add -t http -s user odoo-rpc http://localhost:8084/mcp
```

### Docker Hub

```bash
docker pull vladimirovrosen/odoo-rpc-mcp:latest
docker pull vladimirovrosen/odoo-portainer-mcp:latest
docker pull vladimirovrosen/odoo-claude-terminal:latest
```

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (all platforms)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — `npm install -g @anthropic-ai/claude-code`
- [Anthropic API Key](https://console.anthropic.com)

## MCP Tools Reference

### Native Tools (60)

| Category | Tools | Description |
|----------|-------|-------------|
| **CRUD** | `odoo_search`, `odoo_read`, `odoo_search_read`, `odoo_search_count`, `odoo_create`, `odoo_write`, `odoo_unlink` | Full record operations on any Odoo model |
| **Advanced** | `odoo_execute`, `odoo_report` | Call any model method, generate PDF reports |
| **Introspection** | `odoo_version`, `odoo_list_models`, `odoo_fields_get` | Model discovery and field definitions |
| **Connections** | `odoo_connect`, `odoo_disconnect`, `odoo_connections`, `odoo_refresh` | Multi-instance connection management |
| **Fiscal Positions** | `odoo_fp_list`, `odoo_fp_details`, `odoo_fp_configure`, `odoo_fp_remove_action`, `odoo_fp_types` | Bulgarian localization tax configuration |
| **Gmail** | `google_gmail_search`, `google_gmail_read`, `google_gmail_send`, `google_gmail_labels` | Full Gmail access with OAuth2 |
| **Google Auth** | `google_auth`, `google_auth_status` | OAuth2 authentication for Google services |
| **Calendar** | `google_calendar_list`, `google_calendar_events`, `google_calendar_create_event`, `google_calendar_update_event`, `google_calendar_delete_event` | Google Calendar management |
| **Telegram** | `telegram_send_message`, `telegram_get_messages`, `telegram_search_contacts`, `telegram_get_dialogs`, `telegram_configure`, `telegram_auth`, `telegram_auth_status` | Personal Telegram messaging |
| **SSH** | `ssh_execute` | Run commands on remote servers via SSH |
| **Git** | `git_remote` | Pull/status/log/branch/diff on remote repos via SSH |
| **GitHub API** | `github_api` | Direct GitHub REST API calls |
| **Identity** | `identify`, `who_am_i` | Per-user session identity management |
| **User Connections** | `user_connection_add`, `user_connection_list`, `user_connection_activate`, `user_connection_delete` | Per-user personal connection storage |
| **Memory** | `memory_list`, `memory_read`, `memory_write`, `memory_delete`, `memory_share`, `memory_pull` | Shared knowledge base (`*` for bulk operations) |
| **Proxy** | `proxy_call`, `proxy_discover`, `proxy_refresh` | Manual proxy control and re-discovery |
| **GUI** | `open_connection_manager` | Launch desktop Connection Manager |

### Proxied: Portainer (39 tools, prefix: `portainer__`)

| Category | Tools |
|----------|-------|
| **Environments** | `listEnvironments`, `updateEnvironmentTags`, `updateEnvironmentTeamAccesses`, `updateEnvironmentUserAccesses` |
| **Stacks** | `listLocalStacks`, `createLocalStack`, `updateLocalStack`, `startLocalStack`, `stopLocalStack`, `deleteLocalStack`, `getLocalStackFile`, `listStacks`, `createStack`, `updateStack`, `getStackFile` |
| **Docker Proxy** | `dockerProxy` — full Docker Engine API |
| **Kubernetes** | `kubernetesProxy`, `getKubernetesResourceStripped` |
| **Management** | Access groups, environment groups, tags, teams, users, settings |

### Proxied: GitHub (20 tools, prefix: `github__`)

| Category | Tools |
|----------|-------|
| **Search** | `search_repositories`, `search_code`, `search_issues`, `search_pull_requests`, `search_users` |
| **Repos** | `list_branches`, `list_tags`, `list_commits`, `list_releases`, `get_file_contents` |
| **Issues & PRs** | `list_issues`, `issue_read`, `list_pull_requests`, `pull_request_read` |
| **Other** | `get_me`, `get_commit`, `get_label`, `get_tag`, `get_latest_release`, `get_release_by_tag` |

### Proxied: Microsoft Teams (6 tools, prefix: `teams__`)

| Category | Tools |
|----------|-------|
| **Threads** | `start_thread`, `update_thread`, `read_thread`, `list_threads` |
| **Members** | `get_member_by_name`, `list_members` |

## Connection Manager GUI

| Platform | Toolkit | Install |
|----------|---------|---------|
| Linux (GNOME) | GTK4/libadwaita | `python tools/odoo_connect.py` |
| Windows | Qt6/PySide6 | Download from [Releases](https://github.com/rosenvladimirov/odoo-claude-mcp/releases) |
| macOS | Qt6/PySide6 | `pip install PySide6 && python tools/odoo_connect_qt.py` |

## Authentication

| Mode | Use case | How it works |
|------|----------|--------------|
| **Local** | `localhost` / Docker internal | No token required |
| **API Token** | Public-facing server | `X-Api-Token` header or `?token=` query param |
| **OAuth 2.0** | claude.ai remote MCP | Bearer token flow |
| **Per-user identity** | Multi-user | `identify` tool loads personal connections |

## Security

- **Dual-network isolation** — backend services have no host ports, accessible only via proxy
- All containers run as non-root users (`mcp` user)
- SSH keys mounted read-only, SSH agent forwarded
- API keys preferred over passwords
- OAuth 2.0 for cloud-hosted access (claude.ai)
- `.env` and `proxy_services.json` are in `.gitignore`
- Portainer supports read-only mode
- Session tracking prevents connection conflicts

## Configuration

See [`.env.example`](.env.example) for all environment variables and [`proxy_services.json.example`](proxy_services.json.example) for plugin configuration.

### File Structure

```
odoo-claude-mcp/
├── docker-compose.yml          # All services (public + backend networks)
├── proxy_services.json         # Plugin config (which backends to proxy)
├── .env                        # Credentials (gitignored)
├── install.sh / install.ps1    # One-command installers
│
├── odoo-rpc-mcp/               # Gateway server (60 native tools)
│   ├── Dockerfile
│   ├── server.py               # Tools + proxy + auth + landing page
│   ├── google_service.py       # Gmail + Calendar
│   ├── telegram_service.py     # Telegram (Telethon)
│   └── requirements.txt
│
├── portainer-mcp/              # Backend: Docker management
│   └── Dockerfile              # portainer-mcp + supergateway
│
├── teams-mcp/                  # Backend: MS Teams
│   └── Dockerfile              # InditexTech + supergateway
│
├── github-mcp/                 # Backend: GitHub (official image)
│   └── Dockerfile
│
├── claude-terminal/            # Web terminal (ttyd + Claude Code)
│   ├── Dockerfile
│   ├── .mcp.json               # Internal MCP endpoints
│   └── CLAUDE.md
│
├── tools/                      # Desktop utilities
│   ├── odoo_connect_qt.py      # Qt6 Connection Manager
│   ├── odoo_module_analyzer.py # Module → memory generator
│   └── glb_viewer.py           # 3D GLB viewer
│
└── packaging/windows/          # Windows installer (NSIS)
```

## Docker Hub

| Image | Description |
|-------|-------------|
| [`vladimirovrosen/odoo-rpc-mcp`](https://hub.docker.com/r/vladimirovrosen/odoo-rpc-mcp) | Gateway server (60 native + proxy) |
| [`vladimirovrosen/odoo-portainer-mcp`](https://hub.docker.com/r/vladimirovrosen/odoo-portainer-mcp) | Portainer MCP wrapper |
| [`vladimirovrosen/odoo-claude-terminal`](https://hub.docker.com/r/vladimirovrosen/odoo-claude-terminal) | Web terminal with Claude Code |

## License

[AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)

## Author

**BL Consulting** — [www.bl-consulting.net](https://www.bl-consulting.net)

Developed by Rosen Vladimirov ([rosenvladimirov](https://github.com/rosenvladimirov))
