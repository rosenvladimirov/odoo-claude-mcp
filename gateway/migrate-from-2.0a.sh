#!/bin/bash
#
# Migration script: claude-terminal Phase 2.0a → Phase 2.2
# ========================================================
#
# Phase 2.0a stored each user's workspace at:
#     /var/lib/docker/volumes/odoo-rpc-mcp_claude-user-data/_data/<HASH>
# where <HASH> = HMAC-SHA256(odoo_url|db|login)[:32] keyed by MCP_TENANT_SECRET.
#
# Phase 2.2 stores each workspace on the host at:
#     /srv/claude-terminal-shared/users/<MCP_PROFILE>
# where MCP_PROFILE is whatever the MCP server returns from
# /api/user/register-connection. The two namespaces are different — we
# cannot map HASH → MCP_PROFILE without round-tripping each user's
# (url, db, login) tuple through the MCP server.
#
# This script:
#   1. Lists all <HASH> dirs in the legacy volume.
#   2. For each one, prompts the operator for the corresponding (url, db, login)
#      OR reads from a CSV file (--mapping users.csv with columns: hash,url,db,login).
#   3. Calls MCP /api/user/register-connection (with the user's existing API key
#      from connections.json or admin override) to resolve MCP_PROFILE.
#   4. Moves the dir to /srv/claude-terminal-shared/users/<MCP_PROFILE>.
#   5. chown 1000:1000 + chmod 700.
#
# Run modes:
#   --dry-run        Preview moves without doing them.
#   --interactive    Prompt for each unmapped hash (default).
#   --mapping FILE   CSV file with hash→url,db,login,api_key columns.
#   --auto           Use MCP admin API to enumerate all profiles and match.
#                    (Requires MCP_ADMIN_TOKEN.)
#
# Exit codes:
#   0  all migrated
#   1  partial — some hashes had no mapping (left in place)
#   2  fatal — MCP unreachable, no /srv path, etc.
#
set -euo pipefail

LEGACY_PATH="${LEGACY_PATH:-/var/lib/docker/volumes/odoo-rpc-mcp_claude-user-data/_data}"
TARGET_PATH="${TARGET_PATH:-/srv/claude-terminal-shared/users}"
MCP_BASE="${MCP_BASE:-http://localhost:8084}"

DRY_RUN=0
MAPPING_FILE=""
INTERACTIVE=1

# ── arg parse ───────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)        DRY_RUN=1; shift ;;
        --mapping)        MAPPING_FILE="$2"; INTERACTIVE=0; shift 2 ;;
        --interactive)    INTERACTIVE=1; shift ;;
        --legacy-path)    LEGACY_PATH="$2"; shift 2 ;;
        --target-path)    TARGET_PATH="$2"; shift 2 ;;
        --mcp-base)       MCP_BASE="$2"; shift 2 ;;
        -h|--help)
            sed -n '/^# Migration script/,/^set/p' "$0" | head -40
            exit 0 ;;
        *)
            echo "Unknown arg: $1" >&2
            exit 2 ;;
    esac
done

# ── precondition checks ─────────────────────────────────────────
if [ ! -d "$LEGACY_PATH" ]; then
    echo "FATAL: legacy path $LEGACY_PATH does not exist" >&2
    exit 2
fi

if ! curl -sf "$MCP_BASE/health" > /dev/null; then
    echo "FATAL: MCP at $MCP_BASE is not reachable" >&2
    exit 2
fi

mkdir -p "$TARGET_PATH"
chown 0:1000 "$TARGET_PATH"
chmod 1733 "$TARGET_PATH"

# ── enumerate legacy hashes ─────────────────────────────────────
mapfile -t HASHES < <(find "$LEGACY_PATH" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | sort)
echo "[migrate] found ${#HASHES[@]} legacy workspace(s)"
[ "${#HASHES[@]}" -eq 0 ] && exit 0

# ── helper: register w/ MCP, get profile ────────────────────────
mcp_resolve_profile() {
    local url="$1" db="$2" login="$3" api_key="$4"
    curl -sS -X POST "$MCP_BASE/api/user/register-connection" \
        -H "Content-Type: application/json" \
        -d "$(python3 -c "
import json, sys
print(json.dumps({
    'name': '$login',
    'alias': 'default',
    'url': '$url',
    'db': '$db',
    'login': '$login',
    'api_key': '$api_key',
    'active': True,
}))
")" | python3 -c "import json, sys; d=json.load(sys.stdin); print(d.get('profile') or d.get('owner') or '')"
}

# ── load mapping if given ───────────────────────────────────────
declare -A MAP_URL MAP_DB MAP_LOGIN MAP_KEY
if [ -n "$MAPPING_FILE" ]; then
    if [ ! -r "$MAPPING_FILE" ]; then
        echo "FATAL: cannot read mapping file $MAPPING_FILE" >&2
        exit 2
    fi
    while IFS=, read -r hash url db login api_key; do
        [ -z "${hash:-}" ] && continue
        [ "$hash" = "hash" ] && continue   # header row
        MAP_URL["$hash"]="$url"
        MAP_DB["$hash"]="$db"
        MAP_LOGIN["$hash"]="$login"
        MAP_KEY["$hash"]="$api_key"
    done < "$MAPPING_FILE"
    echo "[migrate] loaded ${#MAP_URL[@]} mapping rows from $MAPPING_FILE"
fi

# ── per-hash migration ──────────────────────────────────────────
MIGRATED=0
SKIPPED=0
for hash in "${HASHES[@]}"; do
    if [ -n "${MAP_URL[$hash]:-}" ]; then
        url="${MAP_URL[$hash]}"
        db="${MAP_DB[$hash]}"
        login="${MAP_LOGIN[$hash]}"
        api_key="${MAP_KEY[$hash]}"
    elif [ "$INTERACTIVE" -eq 1 ]; then
        echo ""
        echo "── unmapped hash: $hash ──"
        echo "  Files:" ; ls -la "$LEGACY_PATH/$hash" | head -5
        read -r -p "  Odoo URL (blank = skip): " url
        [ -z "$url" ] && { echo "  → skipped"; SKIPPED=$((SKIPPED+1)); continue; }
        read -r -p "  Odoo DB: " db
        read -r -p "  User login: " login
        read -r -p "  API key (blank = use admin): " api_key
    else
        echo "[migrate] no mapping for $hash, skipping"
        SKIPPED=$((SKIPPED+1))
        continue
    fi

    profile=$(mcp_resolve_profile "$url" "$db" "$login" "$api_key" || true)
    if [ -z "$profile" ]; then
        echo "[migrate] $hash: MCP did not return a profile (auth fail?)"
        SKIPPED=$((SKIPPED+1))
        continue
    fi

    target="$TARGET_PATH/$profile"
    if [ -d "$target" ]; then
        echo "[migrate] $hash → $profile: target already exists, leaving legacy in place"
        SKIPPED=$((SKIPPED+1))
        continue
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] would: mv $LEGACY_PATH/$hash → $target"
    else
        mv "$LEGACY_PATH/$hash" "$target"
        chown -R 1000:1000 "$target"
        chmod 700 "$target"
        echo "[migrate] $hash → $profile (chown 1000:1000 chmod 700)"
    fi
    MIGRATED=$((MIGRATED+1))
done

echo ""
echo "[migrate] done — migrated=$MIGRATED skipped=$SKIPPED"
[ "$SKIPPED" -gt 0 ] && exit 1 || exit 0
