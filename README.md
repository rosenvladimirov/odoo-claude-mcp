# odoo-claude-mcp

Docker-based MCP server stack for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — integrates **Odoo ERP**, **Docker/Portainer**, **GitHub**, **SSH remote execution**, **Gmail**, **Google Calendar**, and **Telegram** into a unified AI workflow via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

> **107 MCP tools** — manage your entire Odoo infrastructure, communicate with your team, and deploy containers using natural language.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Claude Code CLI / Web Terminal / IDE Extensions / claude.ai             │
│  "show unpaid invoices" · "restart staging" · "git pull on server"      │
└────────┬──────────────────┬──────────────────┬───────────────────────────┘
         │ MCP (HTTP)       │ MCP (SSE)        │ MCP (HTTP)
┌────────▼────────┐ ┌───────▼────────┐ ┌───────▼────────┐
│ odoo-rpc-mcp    │ │ portainer-mcp  │ │ github-mcp     │
│ :8084           │ │ :8085          │ │ :8086          │
│ 49 tools        │ │ 38 tools       │ │ 20 tools       │
│                 │ │                │ │                │
│ Odoo CRUD       │ │ Docker/K8s    │ │ Repos, Issues  │
│ Gmail/Calendar  │ │ Stacks, Envs  │ │ PRs, Branches  │
│ Telegram        │ │ Containers    │ │ Code Search    │
│ SSH Remote      │ │                │ │                │
│ Git Remote      │ │                │ │                │
│ GitHub API      │ │                │ │                │
│ User Identity   │ │                │ │                │
│ Connection GUI  │ │                │ │                │
└────────┬────────┘ └───────┬────────┘ └───────┬────────┘
         │                  │                  │
    Odoo 8–19+        Portainer CE/EE      GitHub API
    SSH Servers        Docker Engine        REST v3
    Google APIs        Kubernetes
    Telegram API
```

## Key Features

- **Full Odoo CRUD** — search, read, create, write, delete records in any Odoo model (v8–19+)
- **Multi-protocol** — XML-RPC (Odoo 8+) and JSON-RPC (Odoo 14+)
- **Multi-connection** — manage multiple Odoo instances, switch between them on the fly
- **Per-user identity** — each Claude session identifies its user, loads personal connections
- **Fiscal positions** — list, inspect, configure, and manage Bulgarian tax fiscal positions
- **Gmail & Calendar** — OAuth2 integration: search/read/send emails, manage calendar events
- **Telegram** — search contacts, read/send messages via personal Telegram account
- **SSH remote** — execute commands on remote servers, run git operations over SSH
- **GitHub API** — direct REST API access for repository management
- **Docker management** — full container/stack/environment control via Portainer
- **Connection Manager GUI** — desktop app (GTK4 on Linux, Qt6 on Windows/macOS)
- **OAuth 2.0 & API tokens** — secure access for cloud-hosted (claude.ai) and local deployments
- **Landing page** — built-in web UI showing server status, endpoints, and setup guide
- **One-command install** — Linux, macOS, and Windows support

## Services

| Service | Port | Transport | Tools | Description |
|---------|------|-----------|-------|-------------|
| `odoo-rpc-mcp` | 8084 | HTTP | 49 | Odoo + Gmail + Calendar + Telegram + SSH + Git + Identity |
| `portainer-mcp` | 8085 | SSE | 38 | Docker/K8s management via Portainer |
| `github-mcp` | 8086 | HTTP | 20 | GitHub repo management (official server) |
| `claude-terminal` | 8080 | — | — | Web terminal (ttyd + Claude Code CLI) |

**Total: 107 MCP tools**

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
docker compose up -d --build
```

### Register MCP with Claude Code

```bash
# Core — Odoo RPC + SSH + Git + Gmail + Telegram
claude mcp add -t http -s user odoo-rpc http://localhost:8084/mcp

# Docker management via Portainer
claude mcp add -t sse -s user portainer http://localhost:8085/sse

# GitHub repos (requires PAT)
claude mcp add -t http -s user github-mcp http://localhost:8086/mcp \
  -H "Authorization: Bearer ghp_YOUR_TOKEN"
```

### Docker Hub

Pre-built images are available on Docker Hub:

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

### Odoo RPC (49 tools)

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
| **GUI** | `open_connection_manager` | Launch desktop Connection Manager |

### Portainer (38 tools)

| Category | Tools | Description |
|----------|-------|-------------|
| **Environments** | `listEnvironments`, `updateEnvironmentTags`, `updateEnvironmentTeamAccesses`, `updateEnvironmentUserAccesses` | Docker host management |
| **Stacks** | `listLocalStacks`, `createLocalStack`, `updateLocalStack`, `startLocalStack`, `stopLocalStack`, `deleteLocalStack`, `getLocalStackFile`, `listStacks`, `createStack`, `updateStack`, `getStackFile` | Docker Compose stack management |
| **Docker Proxy** | `dockerProxy` | Full Docker Engine API (containers, images, volumes, networks) |
| **Kubernetes** | `kubernetesProxy`, `getKubernetesResourceStripped` | K8s cluster management |
| **Management** | Access groups, environment groups, tags, teams, users, settings | Organization and permissions |

### GitHub (20 tools)

| Category | Tools | Description |
|----------|-------|-------------|
| **Search** | `search_repositories`, `search_code`, `search_issues`, `search_pull_requests`, `search_users` | Global GitHub search |
| **Repos** | `list_branches`, `list_tags`, `list_commits`, `list_releases`, `get_file_contents` | Repository browsing |
| **Issues & PRs** | `list_issues`, `issue_read`, `list_pull_requests`, `pull_request_read` | Issue and PR management |
| **Other** | `get_me`, `get_commit`, `get_label`, `get_tag`, `get_latest_release`, `get_release_by_tag` | Metadata access |

## Connection Manager GUI

Desktop app for managing Odoo connections, SSH keys, and session tracking.

| Platform | Toolkit | Install |
|----------|---------|---------|
| Linux (GNOME) | GTK4/libadwaita | `python tools/odoo_connect.py` |
| Windows | Qt6/PySide6 | Download `OdooConnect.exe` from [Releases](https://github.com/rosenvladimirov/odoo-claude-mcp/releases) |
| macOS | Qt6/PySide6 | `pip install PySide6 && python tools/odoo_connect_qt.py` |

**Features:**
- Per-client connections: Odoo server, SSH tunnel, Portainer (with Test buttons)
- Personal profile: GitHub PAT, SSH key management (list, generate, copy)
- Session tracking: see which Claude instance is using which connection
- Self-signed certificate support

## Authentication

The server supports multiple authentication modes for different deployment scenarios:

| Mode | Use case | How it works |
|------|----------|--------------|
| **Local (no auth)** | `localhost` / Docker internal | No token required — trusted network |
| **API Token** | Public-facing server | `X-Api-Token` header or `?token=` query param |
| **OAuth 2.0** | claude.ai remote MCP | Standard Bearer token flow with authorization server |
| **Per-user identity** | Multi-user deployments | `identify` tool loads personal connections per user |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| **Core** | | |
| `ANTHROPIC_API_KEY` | | Anthropic API key (for claude-terminal) |
| `TERMINAL_PORT` | `8080` | Web terminal port |
| **Odoo** | | |
| `ODOO_URL` | | Odoo server URL |
| `ODOO_DB` | | Database name |
| `ODOO_USERNAME` | | Login user |
| `ODOO_PASSWORD` | | Password (or use API key) |
| `ODOO_API_KEY` | | Odoo API key (preferred over password) |
| `ODOO_PROTOCOL` | `xmlrpc` | `xmlrpc` (Odoo 8+) or `jsonrpc` (14+) |
| `ODOO_MCP_PORT` | `8084` | MCP server port |
| **Portainer** | | |
| `PORTAINER_URL` | | Portainer server URL |
| `PORTAINER_TOKEN` | | API token |
| `PORTAINER_READ_ONLY` | | `true` for safe mode |
| `PORTAINER_MCP_PORT` | `8085` | MCP port |
| **GitHub** | | |
| `GITHUB_TOKEN` | | Personal Access Token |
| `GITHUB_MCP_PORT` | `8086` | MCP port |
| **Google** | | |
| `GOOGLE_CREDENTIALS_FILE` | `/data/google_credentials.json` | OAuth2 client credentials |
| `GOOGLE_TOKEN_FILE` | `/data/google_token.json` | Saved OAuth2 token |
| **Telegram** | | |
| `TELEGRAM_API_ID` | | Telegram API ID (from my.telegram.org) |
| `TELEGRAM_API_HASH` | | Telegram API hash |
| `TELEGRAM_SESSION_PATH` | `/data/telegram_session` | Session file path |

### File Structure

```
odoo-claude-mcp/
├── docker-compose.yml          # All services
├── Dockerfile                  # Root Dockerfile (odoo-rpc-mcp standalone)
├── server.py                   # Symlink → odoo-rpc-mcp/server.py
├── .env.example                # Configuration template
├── install.sh                  # Linux/macOS installer
├── install.ps1                 # Windows installer (PowerShell)
│
├── odoo-rpc-mcp/               # Main MCP server (49 tools)
│   ├── Dockerfile
│   ├── server.py               # All tools + landing page + auth
│   ├── google_service.py       # Gmail + Calendar OAuth2 integration
│   ├── telegram_service.py     # Telegram client (Telethon)
│   └── requirements.txt
│
├── claude-terminal/            # Web terminal
│   ├── Dockerfile
│   ├── .mcp.json               # MCP endpoints (internal Docker network)
│   ├── CLAUDE.md               # Domain knowledge for Claude
│   └── settings.json
│
├── portainer-mcp/              # Portainer MCP wrapper
│   └── Dockerfile              # portainer-mcp binary + supergateway
│
├── github-mcp/                 # GitHub MCP (official image)
│   └── Dockerfile
│
├── packaging/                  # Installers
│   └── windows/
│       ├── build.sh            # Docker cross-compile
│       └── installer.nsi       # NSIS installer script
│
├── tools/                      # Desktop utilities
│   ├── odoo_connect_qt.py      # Qt6 Connection Manager (Win/Mac/Linux)
│   ├── odoo_module_analyzer.py # Module → Claude memory generator
│   └── glb_viewer.py           # 3D GLB model viewer
│
└── .github/workflows/
    └── build-windows.yml       # CI: build .exe + installer on tag push
```

## Usage Examples

```
# Odoo — ERP operations
Show me all unpaid customer invoices
Create partner "ACME Corp" with VAT BG123456789
Confirm sales order SO-0042
List fiscal positions for company "RAYTRON GROUP"

# SSH Remote — server management
Run 'docker ps' on the production server
Check disk space on konex-tiva
Restart nginx on staging

# Git Remote — repository operations
Show git status of l10n-bulgaria on the server
Pull latest changes on /opt/odoo/l10n-bulgaria
Show last 10 commits

# Docker (Portainer) — container orchestration
List all containers on the staging environment
Stop the staging stack and redeploy
Deploy a new stack with this compose file

# GitHub — repository management
List open issues in l10n-bulgaria
Search for "fiscal position" in my code
Show recent pull requests

# Gmail — email operations
Search for emails from "client@example.com" this week
Read the latest unread email
Send a reply to the invoice thread

# Calendar — scheduling
What meetings do I have today?
Create a meeting with Ivan tomorrow at 14:00
Move the Friday demo to Monday

# Telegram — team messaging
Send a message to Lyubomir about the deployment
Show recent messages from the dev group
Search for contacts named "Ivan"
```

## Security

- **Network isolation** — Docker internal network for inter-service communication
- **Never expose port 8080** without authentication in production
- `.env` is in `.gitignore` — credentials stay local
- SSH keys mounted read-only, SSH agent forwarded
- All containers run as non-root users (`mcp` user)
- API keys preferred over passwords
- Portainer supports read-only mode
- GitHub MCP requires Bearer token
- OAuth 2.0 for cloud-hosted access (claude.ai)
- Session tracking prevents connection conflicts
- Per-user identity isolation in multi-user setups

## Docker Hub

| Image | Description |
|-------|-------------|
| [`vladimirovrosen/odoo-rpc-mcp`](https://hub.docker.com/r/vladimirovrosen/odoo-rpc-mcp) | Main MCP server (49 tools) |
| [`vladimirovrosen/odoo-portainer-mcp`](https://hub.docker.com/r/vladimirovrosen/odoo-portainer-mcp) | Portainer MCP wrapper |
| [`vladimirovrosen/odoo-claude-terminal`](https://hub.docker.com/r/vladimirovrosen/odoo-claude-terminal) | Web terminal with Claude Code |

## Releases

Windows `.exe` and installer are auto-built on tag push:
- `OdooConnect.exe` — portable, no installation needed
- `OdooConnectSetup.exe` — Windows installer (Start Menu + Desktop shortcut)

Download from [Releases](https://github.com/rosenvladimirov/odoo-claude-mcp/releases).

## License

[AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)

## Author

**BL Consulting** — [www.bl-consulting.net](https://www.bl-consulting.net)

Developed by Rosen Vladimirov ([rosenvladimirov](https://github.com/rosenvladimirov))
