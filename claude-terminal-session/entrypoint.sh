#!/bin/bash
# claude-terminal-session — per-session ephemeral container entrypoint
#
# This script assumes the gateway has ALREADY:
#   • validated the API_KEY against MCP /api/user/register-connection
#   • resolved MCP_PROFILE
#   • bind-mounted /shared/users/${MCP_PROFILE} → /home/claude
#   • set the env vars below
#
# It does NOT:
#   • re-authenticate (gateway already did)
#   • write /tmp/.claude_auth_vars (no race window — env is direct)
#   • mkdir /data/users/* paths (per-session container has none)

set -euo pipefail

# ── Required from gateway ───────────────────────────────────────
: "${API_KEY:?API_KEY required from gateway}"
: "${ODOO_URL:?ODOO_URL required from gateway}"
: "${ODOO_DB:?ODOO_DB required from gateway}"
: "${USER_LOGIN:?USER_LOGIN required from gateway}"
: "${MCP_PROFILE:?MCP_PROFILE required from gateway}"
: "${MCP_URL:=http://odoo-rpc-mcp:8084}"

# ── Optional from gateway ───────────────────────────────────────
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

# ── Workspace setup (idempotent — only on first session) ────────
# /home/claude is the bind-mounted persistent slice from
# /shared/users/${MCP_PROFILE}/ on the host. First-time provisioning
# templates a few files; subsequent sessions reuse them.
if [ ! -d "/home/claude/.claude" ]; then
    mkdir -p /home/claude/.claude/projects /home/claude/workspace
    echo '{}' > /home/claude/.claude.json
fi

# Defence in depth — tighten home dir mode every session.
chmod 700 /home/claude 2>/dev/null || true

# ── Render fresh .mcp.json with current credentials ─────────────
# Generated each session so headers carry the current Odoo API key.
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

# ── Write session context (read by Claude Code on launch) ───────
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

# ── Welcome banner ──────────────────────────────────────────────
COLS=$(tput cols 2>/dev/null || echo 80)
LINE=$(printf '%*s' "$COLS" '' | tr ' ' '─')
echo ""
echo "  Claude Terminal — isolated session"
echo "$LINE"
echo "  User:     ${USER_NAME} (${USER_LOGIN})"
echo "  Odoo:     ${ODOO_URL}"
echo "  Database: ${ODOO_DB}"
if [ -n "${ODOO_MODEL}" ]; then
    echo "  Context:  ${ODOO_MODEL} #${ODOO_RES_ID}"
fi
echo "  Session:  ephemeral — closes when you close the tab"
echo "$LINE"
echo ""

# ── Launch Claude Code ──────────────────────────────────────────
# When the user closes their browser tab the gateway sends SIGTERM;
# tini propagates it to claude which exits cleanly. The container's
# tmpfs /tmp and /run are then discarded by the Docker engine while
# /home/claude (bind mount) survives intact.
cd /home/claude
exec claude
