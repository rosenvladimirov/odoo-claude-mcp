#!/bin/bash
# Spawned by ttyd when the user's WebSocket connects. Renders the
# welcome banner then exec-replaces itself with the Claude CLI.
#
# Env arrives via ttyd inheritance from entrypoint.sh.

set -e

COLS=$(tput cols 2>/dev/null || echo 80)
LINE=$(printf '%*s' "$COLS" '' | tr ' ' '─')

echo ""
echo "  Claude Terminal — isolated session"
echo "$LINE"
echo "  User:     ${USER_NAME:-} (${USER_LOGIN:-})"
echo "  Odoo:     ${ODOO_URL:-}"
echo "  Database: ${ODOO_DB:-}"
if [ -n "${ODOO_MODEL:-}" ]; then
    echo "  Context:  ${ODOO_MODEL} #${ODOO_RES_ID:-0}"
fi
echo "  Session:  ephemeral — closes when you close the tab"
echo "$LINE"
echo ""

cd /home/claude
exec claude
