#!/bin/bash
# Parse KEY=VALUE arguments (from ttyd ?arg= URL params) into env vars
for arg in "$@"; do
    if [[ "$arg" == *=* ]]; then
        key="${arg%%=*}"
        value="${arg#*=}"
        export "$key"="$value"
    fi
done

# Write session context for Claude
cat > /home/claude/.odoo_session.json << EOF
{
  "odoo_url": "${ODOO_ORIGIN:-}",
  "odoo_db": "${ODOO_DB:-}",
  "odoo_user": "${ODOO_USER:-}",
  "odoo_protocol": "${ODOO_PROTOCOL:-xmlrpc}",
  "model": "${ODOO_MODEL:-}",
  "res_id": "${ODOO_RES_ID:-0}"
}
EOF

exec claude
