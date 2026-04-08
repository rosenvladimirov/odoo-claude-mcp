#!/bin/bash
# Multi-user Claude Terminal — Odoo API Key Authentication
# Receives URL parameters via ttyd --url-arg as KEY=VALUE arguments.
# Validates the API key against Odoo xmlrpc, provisions a per-user
# directory, and launches Claude Code with HOME set to that directory.

# ── Parse KEY=VALUE arguments from ttyd URL params ──────────────
for arg in "$@"; do
    if [[ "$arg" == *=* ]]; then
        key="${arg%%=*}"
        value="${arg#*=}"
        export "$key"="$value"
    fi
done

# ── Validate required parameters ────────────────────────────────
if [ -z "$API_KEY" ] || [ -z "$ODOO_URL" ] || [ -z "$ODOO_DB" ]; then
    echo "╔══════════════════════════════════════════╗"
    echo "║  Authentication required                 ║"
    echo "╠══════════════════════════════════════════╣"
    echo "║  Missing: API_KEY, ODOO_URL, or ODOO_DB ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "Usage: ?arg=API_KEY=xxx&arg=ODOO_URL=https://...&arg=ODOO_DB=mydb&arg=ODOO_USER=admin"
    read -r -p "Press Enter to exit..." _
    exit 1
fi

# ── Authenticate against Odoo via xmlrpc ────────────────────────
echo "Authenticating against ${ODOO_URL}..."

AUTH_RESPONSE=$(python3 << 'PYEOF'
import xmlrpc.client, json, sys, ssl, os

odoo_url = os.environ.get('ODOO_URL', '')
odoo_db = os.environ.get('ODOO_DB', '')
odoo_user = os.environ.get('ODOO_USER', 'admin')
api_key = os.environ.get('API_KEY', '')

if not odoo_url or not odoo_db or not api_key:
    print(json.dumps({'error': f'Missing params: url={bool(odoo_url)} db={bool(odoo_db)} key={bool(api_key)}'}))
    sys.exit(1)

ctx = ssl._create_unverified_context()

try:
    common = xmlrpc.client.ServerProxy(odoo_url + '/xmlrpc/2/common', allow_none=True, context=ctx)
    uid = common.authenticate(odoo_db, odoo_user, api_key, {})
    if not uid:
        print(json.dumps({'error': 'Invalid API key or credentials'}))
        sys.exit(1)

    obj = xmlrpc.client.ServerProxy(odoo_url + '/xmlrpc/2/object', allow_none=True, context=ctx)
    users = obj.execute_kw(odoo_db, uid, api_key, 'res.users', 'read', [[uid]],
                           {'fields': ['login', 'name', 'email', 'partner_id']})
    u = users[0] if users else {}
    result = {
        'uid': uid,
        'login': u.get('login', odoo_user),
        'name': u.get('name', odoo_user),
        'email': u.get('email', ''),
    }
    print(json.dumps(result))
except Exception as e:
    print(json.dumps({'error': str(e)}))
    sys.exit(1)
PYEOF
)

# ── Check and parse auth result ─────────────────────────────────
if [ -z "$AUTH_RESPONSE" ]; then
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  Authentication FAILED — no response     ║"
    echo "╚══════════════════════════════════════════╝"
    read -r -p "Press Enter to exit..." _
    exit 1
fi

# Check for error using grep
if echo "$AUTH_RESPONSE" | grep -q '"error"'; then
    ERROR_MSG=$(echo "$AUTH_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('error','Unknown'))" 2>/dev/null || echo "Auth failed")
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  Authentication FAILED                   ║"
    echo "╠══════════════════════════════════════════╣"
    echo "  $ERROR_MSG"
    echo "╚══════════════════════════════════════════╝"
    read -r -p "Press Enter to exit..." _
    exit 1
fi

# ── Extract user info (env var → python, no pipe+heredoc conflict) ──
export _AUTH_JSON="$AUTH_RESPONSE"
python3 << 'PARSEEOF' > /tmp/.claude_auth_vars
import json, os, shlex
d = json.loads(os.environ["_AUTH_JSON"])
print(f'USER_LOGIN={shlex.quote(str(d.get("login", "user")))}')
print(f'USER_NAME={shlex.quote(str(d.get("name", "User")))}')
print(f'USER_UID={shlex.quote(str(d.get("uid", 0)))}')
print(f'USER_EMAIL={shlex.quote(str(d.get("email", "")))}')
PARSEEOF
source /tmp/.claude_auth_vars
rm -f /tmp/.claude_auth_vars
unset _AUTH_JSON

# Sanitize for directory: {db}__{login} — unique per instance+user
# Replace @ . + : / with _, keep alphanumeric + underscore + hyphen, lowercase
SAFE_DB=$(echo "$ODOO_DB" | sed 's/[@.+:\/]/_/g' | tr -cd 'a-zA-Z0-9_-' | tr '[:upper:]' '[:lower:]')
SAFE_LOGIN=$(echo "$USER_LOGIN" | sed 's/[@.+]/_/g' | tr -cd 'a-zA-Z0-9_-' | tr '[:upper:]' '[:lower:]')

# Fallbacks
[ -z "$SAFE_DB" ] && SAFE_DB="default"
[ -z "$SAFE_LOGIN" ] && SAFE_LOGIN="user_${USER_UID}"

SAFE_USER="${SAFE_DB}__${SAFE_LOGIN}"
USER_DIR="/data/users/${SAFE_USER}"

COLS=$(tput cols 2>/dev/null || echo 80)
LINE=$(printf '%*s' "$COLS" '' | tr ' ' '─')
echo ""
echo "  Claude Terminal"
echo "$LINE"
echo "  User:     ${USER_NAME} (${USER_LOGIN})"
echo "  Odoo:     ${ODOO_URL}"
echo "  Database: ${ODOO_DB}"
if [ -n "${ODOO_MODEL:-}" ]; then
    echo "  Context:  ${ODOO_MODEL} #${ODOO_RES_ID:-0}"
fi
echo "$LINE"
echo ""

# ── Provision per-user directory ────────────────────────────────
if [ ! -d "$USER_DIR/.claude" ]; then
    echo "Setting up your workspace for the first time..."
    mkdir -p "$USER_DIR/.claude/projects" "$USER_DIR/workspace"
    # Copy templates
    cp /home/claude/template/settings.json "$USER_DIR/.claude/settings.json" 2>/dev/null
    cp /home/claude/template/.mcp.json "$USER_DIR/.mcp.json" 2>/dev/null
    cp /home/claude/template/CLAUDE.md "$USER_DIR/CLAUDE.md" 2>/dev/null
    # Initialize empty Claude state
    echo '{}' > "$USER_DIR/.claude.json"
    echo "Workspace ready."
fi

# Always refresh rules and MCP config from templates
cp /home/claude/template/CLAUDE.md "$USER_DIR/CLAUDE.md" 2>/dev/null
cp /home/claude/template/.mcp.json "$USER_DIR/.mcp.json" 2>/dev/null

# ── Set per-user environment ────────────────────────────────────
export HOME="$USER_DIR"
export USER_EMAIL USER_NAME USER_LOGIN SAFE_USER
export ODOO_URL ODOO_DB
export ODOO_USER="${ODOO_USER:-admin}"
export ODOO_API_KEY="$API_KEY"
export ODOO_UID="$USER_UID"

# ── Register session with MCP server ───────────────────────────
MCP_URL="${MCP_URL:-http://odoo-rpc-mcp:8084}"
SESSION_ID=""

SESSION_RESPONSE=$(curl -s -X POST "${MCP_URL}/api/session/register" \
    -H "Content-Type: application/json" \
    -d "$(cat <<JSON
{
  "connection_alias": "default",
  "odoo_url": "${ODOO_URL}",
  "odoo_db": "${ODOO_DB}",
  "odoo_username": "${ODOO_USER}",
  "model": "${ODOO_MODEL:-}",
  "res_id": ${ODOO_RES_ID:-0},
  "view_type": "${ODOO_VIEW_TYPE:-form}",
  "terminal_url": "${TERMINAL_URL:-}",
  "user_login": "${USER_LOGIN}",
  "user_name": "${USER_NAME}"
}
JSON
)" 2>/dev/null || true)

SESSION_ID=$(echo "$SESSION_RESPONSE" | grep -o '"session_id":[^,}]*' | cut -d'"' -f4)
export CLAUDE_SESSION_ID="$SESSION_ID"

# ── Identify with MCP server & link shared data ──────────────
IDENTIFY_RESPONSE=$(curl -s -X POST "${MCP_URL}/api/identify" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${USER_NAME}\"}" 2>/dev/null || true)

MCP_PROFILE=$(echo "$IDENTIFY_RESPONSE" | grep -o '"profile":"[^"]*"' | cut -d'"' -f4)
if [ -n "$MCP_PROFILE" ]; then
    SHARED_USER_DIR="/shared-data/users/${MCP_PROFILE}"
    SHARED_MEMORY_DIR="/shared-data/memory/users/${MCP_PROFILE}"
    # Create symlinks in user HOME to shared MCP data
    mkdir -p "$SHARED_USER_DIR" "$SHARED_MEMORY_DIR" 2>/dev/null || true
    ln -sfn "$SHARED_USER_DIR" "$USER_DIR/mcp-data" 2>/dev/null || true
    ln -sfn "$SHARED_MEMORY_DIR" "$USER_DIR/mcp-memory" 2>/dev/null || true
    # Also link connections.json if it exists
    if [ -f "$SHARED_USER_DIR/connections.json" ]; then
        ln -sfn "$SHARED_USER_DIR/connections.json" "$USER_DIR/.odoo_connections.json" 2>/dev/null || true
    fi
    echo "  MCP:      Identified as ${MCP_PROFILE}"
fi

# ── Write session context ───────────────────────────────────────
cat > "$USER_DIR/.odoo_session.json" << EOF
{
  "session_id": "${SESSION_ID}",
  "odoo_url": "${ODOO_URL}",
  "odoo_db": "${ODOO_DB}",
  "odoo_user": "${ODOO_USER}",
  "odoo_uid": ${USER_UID},
  "odoo_api_key_hint": "${API_KEY:0:8}...",
  "odoo_protocol": "xmlrpc",
  "user_login": "${USER_LOGIN}",
  "user_name": "${USER_NAME}",
  "user_email": "${USER_EMAIL}",
  "model": "${ODOO_MODEL:-}",
  "res_id": "${ODOO_RES_ID:-0}",
  "view_type": "${ODOO_VIEW_TYPE:-form}"
}
EOF

# ── Apply terminal theme via OSC escape sequences ─────────────
if [ -n "${CLAUDE_THEME:-}" ]; then
    python3 << 'THEMEEOF'
import json, os, sys

theme_name = os.environ.get('CLAUDE_THEME', '')
if not theme_name:
    sys.exit(0)

themes_path = '/home/claude/themes.json'
if not os.path.exists(themes_path):
    sys.exit(0)

with open(themes_path) as f:
    themes = json.load(f)

theme = themes.get(theme_name)
if not theme:
    names = ', '.join(sorted(themes.keys()))
    print(f"  Unknown theme: {theme_name}")
    print(f"  Available: {names}")
    sys.exit(0)

COLOR_MAP = {
    'black': 0, 'red': 1, 'green': 2, 'yellow': 3,
    'blue': 4, 'magenta': 5, 'cyan': 6, 'white': 7,
    'brightBlack': 8, 'brightRed': 9, 'brightGreen': 10, 'brightYellow': 11,
    'brightBlue': 12, 'brightMagenta': 13, 'brightCyan': 14, 'brightWhite': 15,
}

# OSC 4;N;color — set palette color N
for name, idx in COLOR_MAP.items():
    if name in theme:
        sys.stdout.write(f'\033]4;{idx};{theme[name]}\007')

# OSC 10/11/12 — foreground, background, cursor
if 'foreground' in theme:
    sys.stdout.write(f'\033]10;{theme["foreground"]}\007')
if 'background' in theme:
    sys.stdout.write(f'\033]11;{theme["background"]}\007')
if 'cursor' in theme:
    sys.stdout.write(f'\033]12;{theme["cursor"]}\007')

sys.stdout.flush()
THEMEEOF
fi

# ── Launch Claude Code ──────────────────────────────────────────
cd "$USER_DIR"
exec claude
