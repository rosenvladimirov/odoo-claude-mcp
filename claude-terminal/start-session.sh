#!/bin/bash
# Parse KEY=VALUE arguments (from ttyd ?arg= URL params) into env vars
for arg in "$@"; do
    if [[ "$arg" == *=* ]]; then
        key="${arg%%=*}"
        value="${arg#*=}"
        export "$key"="$value"
    fi
done

# Register this Claude terminal session with the MCP server.
# The MCP server stores the (model, res_id) context in SQLite so that
# when Claude modifies a record, it can send a live refresh bus event
# back to the correct Odoo form/list view.
MCP_URL="${MCP_URL:-http://odoo-rpc-mcp:8084}"
SESSION_ID=""
if [ -n "${ODOO_ORIGIN:-}" ] || [ -n "${ODOO_MODEL:-}" ]; then
    SESSION_RESPONSE=$(curl -s -X POST "${MCP_URL}/api/session/register" \
        -H "Content-Type: application/json" \
        -d "$(cat <<JSON
{
  "connection_alias": "default",
  "odoo_url": "${ODOO_ORIGIN:-}",
  "odoo_db": "${ODOO_DB:-}",
  "odoo_username": "${ODOO_USER:-}",
  "model": "${ODOO_MODEL:-}",
  "res_id": ${ODOO_RES_ID:-0},
  "view_type": "${ODOO_VIEW_TYPE:-form}",
  "terminal_url": "${TERMINAL_URL:-}"
}
JSON
)" 2>/dev/null || true)
    # Extract session_id from JSON response (portable, no jq dependency)
    SESSION_ID=$(echo "$SESSION_RESPONSE" | grep -o '"session_id":[^,}]*' | cut -d'"' -f4)
    export CLAUDE_SESSION_ID="$SESSION_ID"
fi

# Write session context for Claude
cat > /home/claude/.odoo_session.json << EOF
{
  "session_id": "${SESSION_ID}",
  "odoo_url": "${ODOO_ORIGIN:-}",
  "odoo_db": "${ODOO_DB:-}",
  "odoo_user": "${ODOO_USER:-}",
  "odoo_protocol": "${ODOO_PROTOCOL:-xmlrpc}",
  "model": "${ODOO_MODEL:-}",
  "res_id": "${ODOO_RES_ID:-0}",
  "view_type": "${ODOO_VIEW_TYPE:-form}"
}
EOF

exec claude
