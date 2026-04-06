# odoo-claude-mcp

Docker-based MCP server stack for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — integrates Odoo, Portainer (Docker management), and GitHub into a unified AI workflow via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Claude Code CLI / Web Terminal                                    │
│  "show unpaid invoices" · "restart staging" · "list my repos"     │
└────────┬──────────────────┬──────────────────┬─────────────────────┘
         │ MCP (HTTP)       │ MCP (SSE)        │ MCP (HTTP)
┌────────▼────────┐ ┌───────▼────────┐ ┌───────▼────────┐
│ odoo-rpc-mcp    │ │ portainer-mcp  │ │ github-mcp     │
│ :8084           │ │ :8085          │ │ :8086          │
│ ~40 tools       │ │ 38 tools       │ │ 20 tools       │
│ Odoo + Gmail +  │ │ Docker/K8s via │ │ Repos, Issues, │
│ Calendar +      │ │ Portainer API  │ │ PRs, Branches, │
│ Telegram        │ │                │ │ Code Search    │
└────────┬────────┘ └───────┬────────┘ └───────┬────────┘
         │ XML/JSON-RPC     │ REST API         │ GitHub API
    ┌────▼────┐      ┌──────▼──────┐     ┌─────▼─────┐
    │  Odoo   │      │  Portainer  │     │  GitHub   │
    │  8-19+  │      │  CE/EE      │     │  .com     │
    └─────────┘      └─────────────┘     └───────────┘
```

## Services

| Service | Port | Transport | Description |
|---------|------|-----------|-------------|
| `claude-terminal` | 8080 | — | Web terminal (ttyd + Claude Code CLI) |
| `odoo-rpc-mcp` | 8084 | HTTP | Odoo RPC + Gmail + Calendar + Telegram |
| `portainer-mcp` | 8085 | SSE | Docker/K8s management via Portainer |
| `github-mcp` | 8086 | HTTP | GitHub repo management (official server) |

## Quick Start

### Linux / macOS

```bash
curl -fsSL https://raw.githubusercontent.com/rosenvladimirov/odoo-claude-mcp/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/rosenvladimirov/odoo-claude-mcp.git
cd odoo-claude-mcp
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum
docker compose up -d --build
```

### Windows (PowerShell)

```powershell
git clone https://github.com/rosenvladimirov/odoo-claude-mcp.git
cd odoo-claude-mcp
copy .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum
docker compose up -d --build
```

Or run the installer:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

### Prerequisites (all platforms)

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows, macOS, Linux)
- [Git](https://git-scm.com/downloads)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — `npm install -g @anthropic-ai/claude-code`
- [Anthropic API Key](https://console.anthropic.com)

### Register MCP Servers

After starting the services, register them with Claude Code:

```bash
# Core — Odoo RPC (always)
claude mcp add -t http -s user odoo-rpc http://localhost:8084/mcp

# Optional — Portainer (Docker management)
claude mcp add -t sse -s user portainer http://localhost:8085/sse

# Optional — GitHub (repo management, requires PAT)
claude mcp add -t http -s user github-mcp http://localhost:8086/mcp \
  -H "Authorization: Bearer ghp_YOUR_TOKEN"
```

Verify:

```bash
claude mcp list
```

## MCP Tools

### Odoo RPC (~40 tools)

**CRUD:** `odoo_search`, `odoo_read`, `odoo_search_read`, `odoo_create`, `odoo_write`, `odoo_unlink`, `odoo_execute`, `odoo_report`
**Introspection:** `odoo_version`, `odoo_list_models`, `odoo_fields_get`
**Connections:** `odoo_connect`, `odoo_connections`, `odoo_refresh`
**Fiscal Positions:** `odoo_fp_list`, `odoo_fp_details`, `odoo_fp_configure`, `odoo_fp_remove_action`, `odoo_fp_types`
**Google Gmail:** `google_gmail_search`, `google_gmail_read`, `google_gmail_send`, `google_gmail_labels`
**Google Calendar:** `google_calendar_list`, `google_calendar_events`, `google_calendar_create_event`, `google_calendar_update_event`, `google_calendar_delete_event`
**Telegram:** `telegram_send_message`, `telegram_get_messages`, `telegram_search_contacts`, `telegram_get_dialogs`

### Portainer (38 tools)

**Environments:** `listEnvironments`, `updateEnvironmentTags`, `updateEnvironmentTeamAccesses`
**Stacks:** `listLocalStacks`, `createLocalStack`, `updateLocalStack`, `startLocalStack`, `stopLocalStack`, `deleteLocalStack`, `getLocalStackFile`
**Docker Proxy:** `dockerProxy` — full Docker Engine API (containers, images, volumes, networks)
**Kubernetes:** `kubernetesProxy`, `getKubernetesResourceStripped`
**Management:** Access groups, environment groups, tags, teams, users, settings

### GitHub (20 tools)

**Search:** `search_repositories`, `search_code`, `search_issues`, `search_pull_requests`, `search_users`
**Repos:** `list_branches`, `list_tags`, `list_commits`, `list_releases`, `get_file_contents`
**Issues & PRs:** `list_issues`, `issue_read`, `list_pull_requests`, `pull_request_read`
**Other:** `get_me`, `get_commit`, `get_label`, `get_tag`, `get_latest_release`, `get_release_by_tag`

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
| `PORTAINER_URL` | | Portainer server (e.g. `http://192.168.1.100:9000`) |
| `PORTAINER_TOKEN` | | API token |
| `PORTAINER_READ_ONLY` | | Set `true` for safe mode |
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
├── odoo-rpc-mcp/               # Odoo MCP server
│   ├── Dockerfile
│   ├── server.py               # ~40 tools
│   ├── google_service.py       # Gmail + Calendar
│   ├── telegram_service.py     # Telegram integration
│   └── requirements.txt
│
├── portainer-mcp/              # Portainer MCP wrapper
│   └── Dockerfile              # portainer-mcp binary + supergateway
│
├── github-mcp/                 # GitHub MCP (build reference)
│   └── Dockerfile              # Multi-stage build (optional)
│
└── tools/                      # Desktop utilities (Linux)
    ├── odoo_connect.py         # GTK4 connection manager GUI
    ├── odoo_module_analyzer.py # Module → Claude memory generator
    └── glb_viewer.py           # 3D GLB model viewer
```

## Desktop Tools (Linux only)

### Connection Manager GUI

GTK4/libadwaita app with GNOME Settings-style sidebar.

```bash
pip install PyGObject
python tools/odoo_connect.py
```

**Sections:**
- **Personal Profile** — GitHub PAT, SSH key management (generate, list, copy)
- **Per-connection** — Odoo server, SSH tunnel, Portainer (with test buttons)

### Platform Notes

| Feature | Linux | macOS | Windows |
|---------|-------|-------|---------|
| Docker services | Docker / Docker Desktop | Docker Desktop | Docker Desktop |
| Claude Code CLI | npm / standalone | npm / standalone | npm |
| Connection GUI | GTK4 native | — | — |
| Install script | `install.sh` | `install.sh` | `install.ps1` |

> **macOS / Windows:** Configure connections via `.env` file, CLI (`odoo_connect_cli.py`), or ask Claude directly ("connect to my Odoo at ...").

## Usage Examples

```
# Odoo
Show me all unpaid customer invoices from this month
Create partner "ACME Corp" with VAT BG123456789
Confirm sales order SO-0042

# Docker (via Portainer)
List all containers on the server
Stop the staging stack
Show me the compose file for the odoo stack

# GitHub
List my repositories
Show open issues in l10n-bulgaria
Search for "fiscal position" in my code
```

## Security

- **Never expose port 8080 to the internet** without authentication
- `.env` is in `.gitignore` — credentials stay local
- All containers run as non-root users
- API keys preferred over passwords
- Portainer supports read-only mode (`PORTAINER_READ_ONLY=true`)
- GitHub MCP requires Bearer token in HTTP headers

## License

[AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
