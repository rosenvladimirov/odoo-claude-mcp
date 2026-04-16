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
    cp /home/claude/template/settings.json "$USER_DIR/.claude/settings.json" 2>/dev/null
    cp /home/claude/template/CLAUDE.md "$USER_DIR/CLAUDE.md" 2>/dev/null
    echo '{}' > "$USER_DIR/.claude.json"
    echo "Workspace ready."
fi

# Always refresh static rules. .mcp.json is generated dynamically below
# with unified-auth headers — do not copy a stale template over it.
cp /home/claude/template/CLAUDE.md "$USER_DIR/CLAUDE.md" 2>/dev/null

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

# ── Register connection with MCP (unified-auth, task 5) ────────
# Bind (url, db, login, api_key) to a user profile in the MCP registry
# so later unified-auth tool calls can resolve the caller. Payload is
# built in Python to get proper JSON escaping for names with spaces,
# UTF-8, quotes etc.
export ODOO_URL ODOO_DB USER_LOGIN USER_NAME SAFE_DB API_KEY MCP_URL
REGISTER_RESPONSE=$(python3 <<'PYEOF' 2>/dev/null || echo '{"error":"request_failed"}'
import json, os, urllib.request
payload = json.dumps({
    "name":    os.environ.get("USER_NAME", "User"),
    "alias":   os.environ.get("SAFE_DB", "default"),
    "url":     os.environ.get("ODOO_URL", ""),
    "db":      os.environ.get("ODOO_DB", ""),
    "login":   os.environ.get("USER_LOGIN", ""),
    "api_key": os.environ.get("API_KEY", ""),
    "active":  True,
}).encode()
req = urllib.request.Request(
    f"{os.environ['MCP_URL']}/api/user/register-connection",
    data=payload,
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.read().decode())
except urllib.error.HTTPError as e:
    print(e.read().decode())
except Exception as e:
    print(json.dumps({"error": str(e)}))
PYEOF
)

MCP_PROFILE=$(echo "$REGISTER_RESPONSE" | grep -o '"profile":"[^"]*"' | cut -d'"' -f4)
MCP_OWNER=$(echo "$REGISTER_RESPONSE" | grep -o '"owner":"[^"]*"' | cut -d'"' -f4)
if [ -n "$MCP_PROFILE" ]; then
    echo "  MCP:      Registered as ${MCP_PROFILE} (alias=${SAFE_DB})"
elif [ -n "$MCP_OWNER" ]; then
    MCP_PROFILE="$MCP_OWNER"
    echo "  MCP:      Connection bound to profile '${MCP_PROFILE}'"
else
    REG_ERR=$(echo "$REGISTER_RESPONSE" | grep -o '"error":"[^"]*"' | cut -d'"' -f4)
    echo "  MCP:      Registration failed — ${REG_ERR:-unknown}"
    MCP_PROFILE=""
fi

if [ -n "$MCP_PROFILE" ]; then
    SHARED_USER_DIR="/shared-data/users/${MCP_PROFILE}"
    SHARED_MEMORY_DIR="/shared-data/memory/users/${MCP_PROFILE}"
    mkdir -p "$SHARED_USER_DIR" "$SHARED_MEMORY_DIR" 2>/dev/null || true
    ln -sfn "$SHARED_USER_DIR" "$USER_DIR/mcp-data" 2>/dev/null || true
    ln -sfn "$SHARED_MEMORY_DIR" "$USER_DIR/mcp-memory" 2>/dev/null || true
    if [ -f "$SHARED_USER_DIR/connections.json" ]; then
        ln -sfn "$SHARED_USER_DIR/connections.json" "$USER_DIR/.odoo_connections.json" 2>/dev/null || true
    fi
fi

# ── Generate .mcp.json with unified-auth headers (task 5) ──────
# Rebuild the MCP client config fresh every session so headers carry
# the current Odoo API key, URL, DB and login. All subsequent tool
# calls from this Claude CLI instance will authenticate as this user.
python3 <<'MCPEOF' > "$USER_DIR/.mcp.json"
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
        "portainer": {"type": "sse",  "url": "http://portainer-mcp:8085/sse"},
        "github":    {"type": "http", "url": "http://github-mcp:8086/mcp"},
        "teams":     {"type": "sse",  "url": "http://teams-mcp:8087/sse"},
    }
}
print(json.dumps(cfg, indent=2))
MCPEOF
chmod 600 "$USER_DIR/.mcp.json"

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
