# odoo-claude-mcp

Docker-based MCP server stack for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — integrates **Odoo**, **Docker/Portainer**, **GitHub**, **SSH remote execution**, **Gmail**, **Google Calendar**, and **Telegram** into a unified AI workflow via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Claude Code CLI / Web Terminal / IDE Extensions                         │
│  "show unpaid invoices" · "restart staging" · "git pull on server"      │
└────────┬──────────────────┬──────────────────┬───────────────────────────┘
         │ MCP (HTTP)       │ MCP (SSE)        │ MCP (HTTP)
┌────────▼────────┐ ┌───────▼────────┐ ┌───────▼────────┐
│ odoo-rpc-mcp    │ │ portainer-mcp  │ │ github-mcp     │
│ :8084           │ │ :8085          │ │ :8086          │
│ 42 tools        │ │ 38 tools       │ │ 20 tools       │
│                 │ │                │ │                │
│ Odoo CRUD       │ │ Docker/K8s    │ │ Repos, Issues  │
│ Gmail/Calendar  │ │ Stacks, Envs  │ │ PRs, Branches  │
│ Telegram        │ │ Containers    │ │ Code Search    │
│ SSH Remote      │ │                │ │                │
│ Git Remote      │ │                │ │                │
│ GitHub API      │ │                │ │                │
│ Connection GUI  │ │                │ │                │
└────────┬────────┘ └───────┬────────┘ └───────┬────────┘
         │                  │                  │
    Odoo 8-19+        Portainer CE/EE      GitHub API
    SSH Servers        Docker Engine        REST v3
```

## Services

| Service | Port | Transport | Tools | Description |
|---------|------|-----------|-------|-------------|
| `odoo-rpc-mcp` | 8084 | HTTP | 42 | Odoo + Gmail + Calendar + Telegram + SSH + Git + GUI |
| `portainer-mcp` | 8085 | SSE | 38 | Docker/K8s management via Portainer |
| `github-mcp` | 8086 | HTTP | 20 | GitHub repo management (official server) |
| `claude-terminal` | 8080 | — | — | Web terminal (ttyd + Claude Code CLI) |

**Total: 100 MCP tools**

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

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (all platforms)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — `npm install -g @anthropic-ai/claude-code`
- [Anthropic API Key](https://console.anthropic.com)

## MCP Tools Reference

### Odoo RPC (42 tools)

| Category | Tools |
|----------|-------|
| **CRUD** | `odoo_search`, `odoo_read`, `odoo_search_read`, `odoo_search_count`, `odoo_create`, `odoo_write`, `odoo_unlink`, `odoo_execute`, `odoo_report` |
| **Introspection** | `odoo_version`, `odoo_list_models`, `odoo_fields_get` |
| **Connections** | `odoo_connect`, `odoo_connections`, `odoo_refresh` |
| **Fiscal Positions** | `odoo_fp_list`, `odoo_fp_details`, `odoo_fp_configure`, `odoo_fp_remove_action`, `odoo_fp_types` |
| **Gmail** | `google_gmail_search`, `google_gmail_read`, `google_gmail_send`, `google_gmail_labels`, `google_auth`, `google_auth_status` |
| **Calendar** | `google_calendar_list`, `google_calendar_events`, `google_calendar_create_event`, `google_calendar_update_event`, `google_calendar_delete_event` |
| **Telegram** | `telegram_send_message`, `telegram_get_messages`, `telegram_search_contacts`, `telegram_get_dialogs`, `telegram_auth`, `telegram_auth_status`, `telegram_configure` |
| **SSH** | `ssh_execute` — run any command on remote server via SSH |
| **Git** | `git_remote` — pull/status/log/branch/diff on remote repos via SSH |
| **GitHub API** | `github_api` — direct GitHub REST API calls |
| **GUI** | `open_connection_manager` — launch desktop Connection Manager |

### Portainer (38 tools)

| Category | Tools |
|----------|-------|
| **Environments** | `listEnvironments`, `updateEnvironmentTags`, `updateEnvironmentTeamAccesses`, `updateEnvironmentUserAccesses` |
| **Stacks** | `listLocalStacks`, `createLocalStack`, `updateLocalStack`, `startLocalStack`, `stopLocalStack`, `deleteLocalStack`, `getLocalStackFile`, `listStacks`, `createStack`, `updateStack`, `getStackFile` |
| **Docker Proxy** | `dockerProxy` — full Docker Engine API (containers, images, volumes, networks) |
| **Kubernetes** | `kubernetesProxy`, `getKubernetesResourceStripped` |
| **Management** | Access groups, environment groups, tags, teams, users, settings |

### GitHub (20 tools)

| Category | Tools |
|----------|-------|
| **Search** | `search_repositories`, `search_code`, `search_issues`, `search_pull_requests`, `search_users` |
| **Repos** | `list_branches`, `list_tags`, `list_commits`, `list_releases`, `get_file_contents` |
| **Issues & PRs** | `list_issues`, `issue_read`, `list_pull_requests`, `pull_request_read` |
| **Other** | `get_me`, `get_commit`, `get_label`, `get_tag`, `get_latest_release`, `get_release_by_tag` |

## Connection Manager GUI

Desktop app for managing connections, SSH keys, and session tracking.

| Platform | Version | Install |
|----------|---------|---------|
| Linux (GNOME) | GTK4/libadwaita | `python tools/odoo_connect.py` (in claude.ai project) |
| Windows | Qt6/PySide6 | Download `OdooConnect.exe` from [Releases](https://github.com/rosenvladimirov/odoo-claude-mcp/releases) |
| macOS | Qt6/PySide6 | `pip install PySide6 && python tools/odoo_connect_qt.py` |

**Features:**
- Per-client connections: Odoo server, SSH tunnel, Portainer (with Test buttons)
- Personal profile: GitHub PAT, SSH key management (list, generate, copy)
- Session tracking: see which Claude instance is using which connection
- Self-signed certificate support

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| **Core** | | |
| `ANTHROPIC_API_KEY` | | Anthropic API key (required) |
| `TERMINAL_PORT` | `8080` | Web terminal port |
| **Odoo** | | |
| `ODOO_URL` | | Odoo server URL |
| `ODOO_DB` | | Database name |
| `ODOO_USERNAME` | | Login user |
| `ODOO_PASSWORD` | | Password (or use API key) |
| `ODOO_API_KEY` | | Odoo API key (preferred) |
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

### File Structure

```
odoo-claude-mcp/
├── docker-compose.yml          # All services
├── .env.example                # Configuration template
├── install.sh                  # Linux/macOS installer
├── install.ps1                 # Windows installer (PowerShell)
│
├── claude-terminal/            # Web terminal
│   ├── Dockerfile
│   ├── .mcp.json               # MCP endpoints (internal network)
│   ├── CLAUDE.md               # Domain knowledge for Claude
│   └── settings.json
│
├── odoo-rpc-mcp/               # Main MCP server (42 tools)
│   ├── Dockerfile
│   ├── server.py               # All tools implementation
│   ├── google_service.py       # Gmail + Calendar
│   ├── telegram_service.py     # Telegram integration
│   └── requirements.txt
│
├── portainer-mcp/              # Portainer MCP wrapper
│   └── Dockerfile              # portainer-mcp + supergateway
│
├── github-mcp/                 # GitHub MCP (build reference)
│   └── Dockerfile
│
├── packaging/                  # Installers
│   └── windows/
│       ├── build.sh            # Docker cross-compile (reference)
│       └── installer.nsi       # NSIS installer script
│
├── tools/                      # Desktop utilities
│   ├── odoo_connect_qt.py      # Qt6 connection manager (Win/Mac/Linux)
│   ├── odoo_module_analyzer.py # Module → Claude memory generator
│   └── glb_viewer.py           # 3D GLB model viewer
│
└── .github/workflows/
    └── build-windows.yml       # CI: build .exe + installer on tag push
```

## Usage Examples

```
# Odoo
Show me all unpaid customer invoices
Create partner "ACME Corp" with VAT BG123456789
Confirm sales order SO-0042

# SSH Remote
Run 'docker ps' on konex-tiva server
Check disk space on the production server

# Git Remote
Show git status of l10n-bulgaria on konex-tiva
Pull latest changes on /opt/odoo/odoo-19.0/rv/l10n-bulgaria
Show last 10 commits on the server

# Docker (Portainer)
List all containers on konex-tiva
Stop the staging stack
Deploy a new stack with this compose file

# GitHub
List my repositories
Show open issues in l10n-bulgaria
Search for "fiscal position" in my code

# Telegram
Send a message to Lyubomir about the update

# Connection Manager
Open the connection manager GUI
```

## Security

- **Never expose port 8080** without authentication
- `.env` is in `.gitignore` — credentials stay local
- SSH keys mounted read-only, SSH agent forwarded
- All containers run as non-root users
- API keys preferred over passwords
- Portainer supports read-only mode
- GitHub MCP requires Bearer token
- Session tracking prevents connection conflicts

## Releases

Windows `.exe` and installer are auto-built on tag push:
- `OdooConnect.exe` — portable, no installation needed
- `OdooConnectSetup.exe` — Windows installer (Start Menu + Desktop shortcut)

Download from [Releases](https://github.com/rosenvladimirov/odoo-claude-mcp/releases).

## License

[AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
