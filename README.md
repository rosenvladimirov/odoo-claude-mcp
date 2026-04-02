# odoo-claude-mcp

Docker-based bridge between [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and [Odoo](https://www.odoo.com/) via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

Opens a web terminal running Claude Code with full Odoo RPC access — search, read, create, update, delete records, call methods, generate reports, and configure fiscal positions — all through natural language.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser                                                        │
│  ┌───────────────────────────┐   ┌───────────────────────────┐  │
│  │  Odoo 18 (your instance)  │   │  Claude Terminal (:8080)  │  │
│  │                           │   │  xterm.js + Claude Code   │  │
│  │  [AI] button in chatter ──┼──►│                           │  │
│  │  or list view             │   │  claude> "show me all     │  │
│  │                           │   │   unpaid invoices..."     │  │
│  └───────────────────────────┘   └─────────────┬─────────────┘  │
└────────────────────────────────────────────────┼────────────────┘
                                                 │ MCP (HTTP)
                                    ┌────────────▼────────────┐
                                    │  odoo-rpc-mcp (:8084)   │
                                    │  18 tools · XML/JSON-RPC│
                                    │  Multi-connection       │
                                    └────────────┬────────────┘
                                                 │ RPC
                                    ┌────────────▼────────────┐
                                    │  Odoo Instance (:8069)  │
                                    │  Any version 8+         │
                                    └─────────────────────────┘
```

**Two Docker services:**

| Service | Port | Description |
|---------|------|-------------|
| `claude-terminal` | 8080 | [ttyd](https://github.com/tsl0922/ttyd) web terminal running Claude Code CLI |
| `odoo-rpc-mcp` | 8084 | MCP server exposing Odoo RPC operations as 18 tools |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/rosenvladimirov/odoo-claude-mcp.git
cd odoo-claude-mcp
cp .env.example .env
```

Edit `.env`:

```bash
# Required: your Anthropic API key
ANTHROPIC_API_KEY=sk-ant-...

# Optional: pre-configure an Odoo connection
ODOO_URL=https://your-odoo.com
ODOO_DB=your_database
ODOO_USERNAME=admin
ODOO_API_KEY=your_odoo_api_key
ODOO_PROTOCOL=xmlrpc        # xmlrpc (Odoo 8+) or jsonrpc (Odoo 14+)
```

### 2. Start

```bash
docker compose up -d --build
```

### 3. Open the terminal

Navigate to **http://localhost:8080** — Claude Code CLI is ready.

### 4. Connect to Odoo

If you configured `ODOO_*` variables in `.env`, the connection is automatic. Otherwise, ask Claude:

```
Connect to my Odoo at https://my-odoo.com, database "production",
user "admin", API key "abc123"
```

Claude calls `odoo_connect` and establishes the connection. Credentials are saved in `~/odoo-claude-connections/connections.json` for future sessions.

## MCP Tools

### Connection Management

| Tool | Description |
|------|-------------|
| `odoo_connect` | Add or update a named connection |
| `odoo_disconnect` | Remove a connection |
| `odoo_connections` | List all active connections |

### Introspection

| Tool | Description |
|------|-------------|
| `odoo_version` | Get Odoo server version |
| `odoo_list_models` | Search available models by pattern |
| `odoo_fields_get` | Get field definitions for a model |

### CRUD Operations

| Tool | Description |
|------|-------------|
| `odoo_search` | Search records by domain |
| `odoo_read` | Read records by IDs |
| `odoo_search_read` | Combined search + read |
| `odoo_search_count` | Count matching records |
| `odoo_create` | Create one or more records |
| `odoo_write` | Update existing records |
| `odoo_unlink` | Delete records |

### Advanced

| Tool | Description |
|------|-------------|
| `odoo_execute` | Call any model method (`action_confirm`, `button_validate`, etc.) |
| `odoo_report` | Generate PDF reports (returns base64) |

### Fiscal Position Configuration (Bulgarian Localization)

Specialized tools for configuring fiscal positions with tax action maps, designed for the `l10n_bg_tax_admin` module.

| Tool | Description |
|------|-------------|
| `odoo_fp_list` | List fiscal positions with mapping counts |
| `odoo_fp_details` | Full FP config with all tax action entries |
| `odoo_fp_configure` | Add/update a tax action map entry |
| `odoo_fp_remove_action` | Delete a tax action map entry |
| `odoo_fp_types` | Reference data: move types, document types, VAT types |

## Usage Examples

Once connected, talk to Claude in natural language:

```
Show me all unpaid customer invoices from this month

Create a new partner "ACME Corp" with VAT number BG123456789

Confirm sales order SO-0042

What products are running low on stock?

Generate a PDF for invoice INV/2026/0001

List all fiscal positions and their tax mappings
```

## Multi-Connection Support

Manage multiple Odoo instances by alias:

```
Connect to production: https://prod.example.com, db "prod", user "admin", key "xxx"
Connect to staging: https://staging.example.com, db "staging", user "admin", key "yyy"

Show me the partner count on production vs staging
```

Each connection is stored by name in `connections.json` and persists across sessions.

### CLI Connection Manager

The `odoo_connect_cli.py` tool provides command-line management:

```bash
python odoo_connect_cli.py list
python odoo_connect_cli.py add production --url https://prod.example.com --db prod --user admin --api-key xxx --test
python odoo_connect_cli.py test production
python odoo_connect_cli.py delete staging --yes
python odoo_connect_cli.py export backup.json
```

## Odoo Integration (Optional)

The companion Odoo module **`l10n_bg_claude_terminal`** adds an AI button directly in Odoo:

- **Chatter**: Toggle button opens a terminal panel below the form
- **List View**: AI button in the control panel opens a modal terminal

The module passes session context (URL, database, user, current model/record) to the terminal, so Claude knows which record you're looking at.

> The Odoo module is maintained separately in [rosenvladimirov/l10n-bulgaria](https://github.com/rosenvladimirov/l10n-bulgaria).

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | | Anthropic API key for Claude Code |
| `TERMINAL_PORT` | `8080` | Web terminal port |
| `ODOO_MCP_PORT` | `8084` | MCP server port |
| `ODOO_URL` | | Pre-configured Odoo URL |
| `ODOO_DB` | | Pre-configured database name |
| `ODOO_USERNAME` | | Pre-configured username |
| `ODOO_PASSWORD` | | Password (or use `ODOO_API_KEY` instead) |
| `ODOO_API_KEY` | | Odoo API key (preferred over password) |
| `ODOO_PROTOCOL` | `xmlrpc` | `xmlrpc` (Odoo 8+) or `jsonrpc` (Odoo 14+) |
| `WORKSPACE_PATH` | `./_workspace` | Host directory mounted at `/workspace` |
| `CLAUDE_THEME` | `light` | Terminal color theme: `light` or `dark` |
| `SINGLE_CONNECTION` | `false` | Hide connect/disconnect tools, use only "default" |

### Docker Volumes

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| `~/.claude` | `/home/claude/.claude` | Claude Code configuration and memory |
| `~/.claude.json` | `/home/claude/.claude.json` | Claude login state |
| `~/odoo-claude-connections` | `/data` | Shared connection credentials |
| `$WORKSPACE_PATH` | `/workspace` | Project files accessible to Claude |

### Single-Connection Mode

When `ODOO_URL` is set or `SINGLE_CONNECTION=true`, the MCP server:

- Hides `odoo_connect` and `odoo_disconnect` tools
- Uses only the pre-configured "default" connection
- Ideal for embedded/iframe deployments where the connection is set by the host application

## File Structure

```
odoo-claude-mcp/
├── docker-compose.yml          # Service orchestration
├── .env.example                # Configuration template
├── server.py                   # Standalone MCP server (SSE transport)
├── Dockerfile                  # Standalone server image
│
├── claude-terminal/            # Web terminal service
│   ├── Dockerfile              # Node 22 + ttyd (built from source) + Claude Code CLI
│   ├── entrypoint.sh           # ttyd launcher with theme configuration
│   ├── start-session.sh        # Parses URL params → session context → launches claude
│   ├── CLAUDE.md               # Odoo domain knowledge base for Claude
│   ├── settings.json           # Claude Code settings
│   └── .mcp.json               # MCP server endpoint (internal Docker network)
│
├── odoo-rpc-mcp/               # MCP server service
│   ├── Dockerfile              # Python 3.13-slim, non-root user
│   ├── server.py               # 18 MCP tools (CRUD + fiscal positions)
│   ├── requirements.txt        # mcp >= 1.0.0, uvicorn
│   └── odoo_connect_cli.py     # CLI connection manager with SSH support
│
└── tools/                      # Standalone desktop & CLI utilities
    ├── odoo_connect.py         # GTK4 GUI connection manager (GNOME)
    ├── odoo_module_analyzer.py # Odoo module → Claude memory file generator
    └── glb_viewer.py           # GTK4 + OpenGL 3D GLB model viewer
```

## Desktop & CLI Tools

The `tools/` directory contains standalone utilities that complement the MCP server. These run on the **host machine** (not inside Docker).

### Odoo Connection Manager (GUI)

GTK4/libadwaita desktop app for managing Odoo connections — GNOME Settings style with sidebar navigation.

```bash
# Requirements: GTK4, libadwaita
pip install PyGObject
python tools/odoo_connect.py
```

Features:
- Add, edit, delete Odoo connections with a visual interface
- Test connections (XML-RPC authentication)
- SSH configuration per connection (host, user, port, auth method)
- Manage `~/odoo-claude-connections/connections.json` — shared with the MCP server

Set `ODOO_CONNECTIONS_DIR` to override the default path.

### Odoo Module Analyzer

Analyzes an Odoo module's source code and generates a Claude-compatible memory file with XML-RPC operation examples.

```bash
# Requirements
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# Analyze a single module
python tools/odoo_module_analyzer.py /path/to/my_module

# Analyze all modules in a repo
python tools/odoo_module_analyzer.py /path/to/repo --all-modules

# Output to custom directory
python tools/odoo_module_analyzer.py /path/to/module --output ./memory/

# Dry run — see which files would be sent
python tools/odoo_module_analyzer.py /path/to/module --dry-run
```

Generates a markdown file with:
- Module description and key models
- Ready-to-copy XML-RPC operations (`search_read`, `create`, `write`, custom methods)
- Quick command triggers
- Dependencies and limitations

### GLB 3D Viewer

GTK4 + OpenGL viewer for `.glb` (glTF 2.0 binary) 3D model files — useful for inspecting product design assets.

```bash
# Requirements: GTK4, libadwaita, PyOpenGL, numpy, pygltflib
pip install PyGObject PyOpenGL numpy pygltflib
python tools/glb_viewer.py model.glb
```

Features:
- Mouse rotation and zoom
- Auto-color palette for meshes without materials
- Supports positions, normals, and indices

## How It Works

### Session Flow

1. User opens the terminal (directly or via Odoo AI button)
2. `start-session.sh` reads URL parameters (Odoo URL, DB, user, model, record ID)
3. Session context is written to `~/.odoo_session.json`
4. Claude Code CLI starts with MCP configured to reach `odoo-rpc-mcp`
5. Claude reads the session context and auto-connects to the Odoo instance
6. User interacts with Odoo through natural language

### MCP Transport

The MCP server supports both **Streamable HTTP** and **SSE** transports:

- **Container-to-container**: `http://odoo-rpc-mcp:8084/mcp` (Streamable HTTP)
- **Host access**: `http://localhost:8084/mcp` or `http://localhost:8084/sse` (SSE)
- **Health check**: `http://localhost:8084/health`

### Credential Storage

Connections are persisted in `~/odoo-claude-connections/connections.json`:

```json
{
  "default": {
    "url": "https://my-odoo.com",
    "db": "production",
    "user": "admin",
    "api_key": "...",
    "protocol": "xmlrpc"
  }
}
```

This file is shared between both containers via a Docker volume mount.

## Security Notes

- **Do not expose port 8080 to the internet** without authentication. The terminal provides full shell access.
- Credentials are stored on the host filesystem. Protect `~/odoo-claude-connections/` with appropriate permissions.
- Both containers run as non-root users (`claude` uid 1000, `mcp`).
- API keys are preferred over passwords for Odoo authentication.
- In production, use a reverse proxy with TLS and authentication (nginx + OAuth2, Traefik, etc.).

## Requirements

- Docker and Docker Compose v2+
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- An Odoo instance (version 8+) with XML-RPC or JSON-RPC enabled

## License

[AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
