#!/usr/bin/env bash
# Integration tests for the MCP unified-auth flow (tasks 8, 9, 10).
#
# Covers:
#   - register-connection endpoint (positive / negative / conflict)
#   - /api/identify unified-auth vs stdio fallback (spoofing defense +
#     backward compatibility for tool callers without HTTP headers)
#   - XMLRPC validation cache (second call should be ~instant)
#   - ALLOWED_ODOO_URLS whitelist enforcement (task 9)
#   - full cycle: register → identify → registered alias visible
#
# Usage (host):
#   export MCP_URL=http://localhost:8084
#   export ODOO_URL=https://www.odoo-shell.dev
#   export ODOO_DB=odoo-2026
#   export ODOO_LOGIN=vladimirov.rosen@gmail.com
#   export ODOO_KEY=<real key>
#   bash tests/test_unified_auth.sh
#
# The suite does NOT rebuild the container. Whitelist test (task 9)
# expects the operator to set ALLOWED_ODOO_URLS in docker-compose env
# BEFORE running it (otherwise T9 is skipped with a note).

set -u

: "${MCP_URL:=http://localhost:8084}"
: "${ODOO_URL:?set ODOO_URL to a reachable Odoo instance}"
: "${ODOO_DB:?set ODOO_DB}"
: "${ODOO_LOGIN:?set ODOO_LOGIN}"
: "${ODOO_KEY:?set ODOO_KEY to a valid Odoo API key}"

PROFILE="${PROFILE:-IntegrationTest}"
ALIAS="${ALIAS:-integration-$(date +%s)}"

pass=0; fail=0; skipped=0
section() { echo ""; echo "=== $* ==="; }
ok()      { echo "  ✓ $*"; pass=$((pass+1)); }
bad()     { echo "  ✗ $*"; fail=$((fail+1)); }
skip()    { echo "  ~ skipped: $*"; skipped=$((skipped+1)); }

json_field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$1',''))"; }

# Globals populated by post_json: LAST_CODE, LAST_BODY (/tmp/resp.json)
LAST_CODE=""

post_json() {
  # $1=path $2="auth"|"" $3=json_body
  local url="$MCP_URL$1" auth="$2" body="$3"
  local args=(-s -o /tmp/resp.json -w "%{http_code}" -X POST "$url"
              -H "Content-Type: application/json")
  if [ "$auth" = "auth" ]; then
    args+=(-H "Authorization: Bearer $ODOO_KEY"
           -H "X-Odoo-Url: $ODOO_URL"
           -H "X-Odoo-Db: $ODOO_DB"
           -H "X-Odoo-Login: $ODOO_LOGIN")
  elif [ "$auth" = "bad-auth" ]; then
    args+=(-H "Authorization: Bearer wrong-key"
           -H "X-Odoo-Url: $ODOO_URL"
           -H "X-Odoo-Db: $ODOO_DB"
           -H "X-Odoo-Login: $ODOO_LOGIN")
  fi
  args+=(-d "$body")
  LAST_CODE=$(curl "${args[@]}")
}

# ─── T1: register-connection — missing fields ────────────────────────
section "T1: register-connection missing fields"
post_json /api/user/register-connection "" '{}'
if [ "$LAST_CODE" = "400" ]; then ok "missing fields → 400"; else bad "expected 400, got $LAST_CODE"; fi

# ─── T2: register-connection — invalid Odoo creds ────────────────────
section "T2: register-connection invalid key"
post_json /api/user/register-connection "" \
  "{\"name\":\"X\",\"alias\":\"x\",\"url\":\"$ODOO_URL\",\"db\":\"$ODOO_DB\",\"login\":\"$ODOO_LOGIN\",\"api_key\":\"not-a-real-key\"}"
if [ "$LAST_CODE" = "401" ]; then ok "invalid key → 401"; else bad "expected 401, got $LAST_CODE"; fi

# ─── T3: register-connection — valid creds ───────────────────────────
section "T3: register-connection valid"
body="{\"name\":\"$PROFILE\",\"alias\":\"$ALIAS\",\"url\":\"$ODOO_URL\",\"db\":\"$ODOO_DB\",\"login\":\"$ODOO_LOGIN\",\"api_key\":\"$ODOO_KEY\",\"active\":true}"
post_json /api/user/register-connection "" "$body"
if [ "$LAST_CODE" = "200" ]; then
    status=$(json_field status < /tmp/resp.json)
    profile=$(json_field profile < /tmp/resp.json)
    if [ "$status" = "registered" ] && [ -n "$profile" ]; then
        ok "valid register → profile=$profile"
    else bad "200 OK but status=$status profile=$profile"; fi
else bad "expected 200, got $LAST_CODE"; fi

# ─── T4: register-connection — same 4-tuple under ANOTHER profile ────
section "T4: conflict — bind under foreign profile"
post_json /api/user/register-connection "" \
  "{\"name\":\"AnotherName\",\"alias\":\"x\",\"url\":\"$ODOO_URL\",\"db\":\"$ODOO_DB\",\"login\":\"$ODOO_LOGIN\",\"api_key\":\"$ODOO_KEY\"}"
if [ "$LAST_CODE" = "409" ]; then ok "conflict → 409"; else bad "expected 409, got $LAST_CODE (response: $(cat /tmp/resp.json))"; fi

# ─── T5: stdio fallback on /api/identify (task 8) ────────────────────
section "T5: /api/identify without unified-auth headers (stdio compat)"
post_json /api/identify "" '{"name":"StdioUser"}'
if [ "$LAST_CODE" = "200" ]; then
    user=$(json_field user < /tmp/resp.json)
    validated=$(json_field validated < /tmp/resp.json)
    if [ "$user" = "StdioUser" ] && [ "$validated" = "False" ]; then
        ok "body.name accepted, validated=false (task 8 stdio compat)"
    else bad "unexpected user=$user validated=$validated"; fi
else bad "expected 200, got $LAST_CODE"; fi

# ─── T6: spoof defense with unified-auth (task 3) ────────────────────
section "T6: /api/identify with valid auth + spoofed body name"
post_json /api/identify "auth" '{"name":"IvanTheSpoofer"}'
if [ "$LAST_CODE" = "200" ]; then
    user=$(json_field user < /tmp/resp.json)
    validated=$(json_field validated < /tmp/resp.json)
    if [ "$user" != "IvanTheSpoofer" ] && [ "$validated" = "True" ]; then
        ok "spoof ignored, user=$user, validated=true"
    else bad "spoof leaked: user=$user validated=$validated"; fi
else bad "expected 200, got $LAST_CODE"; fi

# ─── T7: /api/identify with wrong key (task 2 middleware) ────────────
section "T7: /api/identify with wrong key"
post_json /api/identify "bad-auth" '{"name":"X"}'
if [ "$LAST_CODE" = "401" ]; then ok "wrong key → 401"; else bad "expected 401, got $LAST_CODE"; fi

# ─── T8: cache hit — two back-to-back valid calls ───────────────────
# Not a strict assertion: remote Odoo latency dominates and varies.
# The second call MUST authorize correctly (success) — timing is
# reported as information, not as a pass/fail criterion.
section "T8: XMLRPC cache hit on repeated valid auth"
t1=$(date +%s%N)
post_json /api/identify "auth" '{}'
code1=$LAST_CODE
t2=$(date +%s%N)
post_json /api/identify "auth" '{}'
code2=$LAST_CODE
t3=$(date +%s%N)
d1=$(( (t2-t1) / 1000000 ))
d2=$(( (t3-t2) / 1000000 ))
if [ "$code1" = "200" ] && [ "$code2" = "200" ]; then
    ok "both calls authorized (first=${d1}ms, second=${d2}ms, cache speedup: $(( d1 - d2 ))ms)"
else
    bad "auth failed: code1=$code1 code2=$code2 (first=${d1}ms, second=${d2}ms)"
fi

# ─── T9: whitelist enforcement (task 9) ─────────────────────────────
section "T9: ALLOWED_ODOO_URLS whitelist enforcement"
wl=$(docker compose -f ~/Проекти/odoo/odoo-mcp/docker-compose.yml exec -T odoo-rpc-mcp \
     sh -c 'echo ${ALLOWED_ODOO_URLS:-__unset__}' 2>/dev/null | tr -d '\r')
if [ -z "$wl" ] || [ "$wl" = "__unset__" ]; then
    skip "ALLOWED_ODOO_URLS not set on odoo-rpc-mcp container — add to .env and restart to run this test"
else
    if echo "$wl" | grep -q "$ODOO_URL"; then
        skip "current ODOO_URL is in the whitelist — whitelist test requires a non-whitelisted URL"
    else
        post_json /api/identify "auth" '{}'
        if [ "$LAST_CODE" = "401" ]; then ok "non-whitelisted URL → 401 (whitelist=$wl)"
        else bad "whitelist bypassed (expected 401, got $LAST_CODE)"; fi
    fi
fi

# ─── T10: full cycle — register then identify ───────────────────────
section "T10: full cycle (register → identify resolves)"
post_json /api/identify "auth" '{}'
user=$(json_field user < /tmp/resp.json)
connections=$(python3 -c "import json; d=json.load(open('/tmp/resp.json')); print(','.join(d.get('connections',[])))")
if echo "$connections" | grep -q "$ALIAS"; then
    ok "alias '$ALIAS' visible for profile '$user'"
else
    bad "alias '$ALIAS' NOT in connections: $connections"
fi

# ─── Summary ─────────────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────"
printf "  passed:  %d\n  failed:  %d\n  skipped: %d\n" "$pass" "$fail" "$skipped"
echo "──────────────────────────────────"
[ "$fail" = "0" ]
