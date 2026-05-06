#!/bin/bash
# claude-terminal-session — per-session container init (PID 1 under tini)
#
# The gateway has already validated the API key and bind-mounted
# /srv/claude-terminal-shared/users/${MCP_PROFILE} → /home/claude.
# This script provisions the workspace if needed, renders the per-session
# .mcp.json with current credentials, then exec-replaces itself with ttyd
# which serves a single WebSocket terminal session.
#
# Container lifecycle:
#   gateway docker run → entrypoint.sh → exec ttyd → ttyd accepts WS →
#   ttyd spawns session-shell.sh → claude CLI runs → user closes tab →
#   ttyd exits → tini reaps → container exits → AutoRemove deletes container

set -euo pipefail

# ── Required env from gateway ───────────────────────────────────
: "${API_KEY:?API_KEY required from gateway}"
: "${ODOO_URL:?ODOO_URL required from gateway}"
: "${ODOO_DB:?ODOO_DB required from gateway}"
: "${USER_LOGIN:?USER_LOGIN required from gateway}"
: "${MCP_PROFILE:?MCP_PROFILE required from gateway}"
export MCP_URL="${MCP_URL:-http://odoo-rpc-mcp:8084}"

# ── Optional env from gateway ───────────────────────────────────
USER_NAME="${USER_NAME:-User}"
USER_EMAIL="${USER_EMAIL:-}"
ODOO_USER="${ODOO_USER:-admin}"
ODOO_UID="${ODOO_UID:-0}"
ODOO_MODEL="${ODOO_MODEL:-}"
ODOO_RES_ID="${ODOO_RES_ID:-0}"
ODOO_VIEW_TYPE="${ODOO_VIEW_TYPE:-form}"
TERMINAL_URL="${TERMINAL_URL:-}"
SESSION_ID="${SESSION_ID:-}"
CLAUDE_THEME="${CLAUDE_THEME:-github}"
TTYD_PORT="${TTYD_PORT:-8081}"

# ── First-session workspace provisioning ────────────────────────
# /home/claude is bind-mounted from /srv/claude-terminal-shared/users/$PROFILE.
# Provision once; subsequent sessions reuse what's there.
if [ ! -d "/home/claude/.claude" ]; then
    mkdir -p /home/claude/.claude/projects /home/claude/workspace
    echo '{}' > /home/claude/.claude.json
fi

# Tighten home dir mode every session — defence in depth.
chmod 700 /home/claude 2>/dev/null || true

# ── Render fresh .mcp.json with current API key ─────────────────
python3 <<MCPEOF > /home/claude/.mcp.json
import json, os
cfg = {
    "mcpServers": {
        "odoo-rpc": {
            "type": "http",
            "url": f"{os.environ['MCP_URL']}/mcp",
            "headers": {
                "Authorization": f"Bearer {os.environ['API_KEY']}",
                "X-Odoo-Url":    os.environ["ODOO_URL"],
                "X-Odoo-Db":     os.environ["ODOO_DB"],
                "X-Odoo-Login":  os.environ["USER_LOGIN"],
            },
        },
    }
}
print(json.dumps(cfg, indent=2))
MCPEOF
chmod 600 /home/claude/.mcp.json

# ── Write session context for Claude / future MCP tools ─────────
cat > /home/claude/.odoo_session.json <<EOF
{
  "session_id": "${SESSION_ID}",
  "odoo_url": "${ODOO_URL}",
  "odoo_db": "${ODOO_DB}",
  "odoo_user": "${ODOO_USER}",
  "odoo_uid": ${ODOO_UID},
  "odoo_api_key_hint": "${API_KEY:0:8}...",
  "odoo_protocol": "xmlrpc",
  "user_login": "${USER_LOGIN}",
  "user_name": "${USER_NAME}",
  "user_email": "${USER_EMAIL}",
  "model": "${ODOO_MODEL}",
  "res_id": "${ODOO_RES_ID}",
  "view_type": "${ODOO_VIEW_TYPE}",
  "mcp_profile": "${MCP_PROFILE}"
}
EOF
chmod 600 /home/claude/.odoo_session.json

# Export the env so session-shell.sh inherits it (ttyd preserves env).
export API_KEY ODOO_URL ODOO_DB USER_LOGIN USER_NAME USER_EMAIL
export ODOO_USER ODOO_UID ODOO_MODEL ODOO_RES_ID ODOO_VIEW_TYPE
export TERMINAL_URL SESSION_ID MCP_PROFILE MCP_URL CLAUDE_THEME

# ── Start ttyd, listen for the single WebSocket connection ──────
# --once exits ttyd after the first connection closes — perfect for the
# per-session model. Container then exits via tini reap and AutoRemove.
exec ttyd \
    --port "${TTYD_PORT}" \
    --writable \
    --once \
    --client-option "titleFixed=Claude Terminal" \
    --client-option "fontSize=13" \
    --client-option "fontFamily=JetBrains Mono,Fira Code,Cascadia Code,monospace" \
    /usr/local/bin/claude-session-shell
