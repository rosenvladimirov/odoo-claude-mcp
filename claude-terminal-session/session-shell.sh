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

# ── ASK-mode focus block (idempotent) ──────────────────────────
# /home/claude е персистентен (bind-mount), затова управляваме само нашия
# делимитиран блок: махаме стария при всяка сесия, добавяме свеж само ако
# ODOO_FOCUS=ask. Не трупа, не пипа потребителските инструкции. Знанието
# (как да отговаря за записа) живее в ai.skill 'ask-about-record' в Odoo —
# тук само насочваме асистента да го зареди.
CLAUDE_MD="/home/claude/CLAUDE.md"
FB="<!-- BEGIN ODOO_FOCUS -->"
FE="<!-- END ODOO_FOCUS -->"
[ -f "$CLAUDE_MD" ] && sed -i "/$FB/,/$FE/d" "$CLAUDE_MD" 2>/dev/null || true
if [ "${ODOO_FOCUS:-}" = "ask" ] && [ -n "${ODOO_MODEL:-}" ]; then
    cat >> "$CLAUDE_MD" <<FOCUSEOF
$FB
## Session focus: ASK MODE

You were opened in ask mode for \`${ODOO_MODEL}\` #\`${ODOO_RES_ID:-0}\`.
Before answering anything:
1. Load the knowledge skill — \`odoo_search_read\` on model \`ai.skill\`,
   domain \`[["name", "=", "ask-about-record"]]\`, fields \`["content"]\` —
   and follow its instructions.
2. Read the record (\`odoo_read\` on \`${ODOO_MODEL}\` id \`${ODOO_RES_ID:-0}\`).
3. Answer the user's questions about THIS record only. Stay read-only unless
   the user explicitly asks for a change.
$FE
FOCUSEOF
fi

cd /home/claude
exec claude
