#!/bin/bash

# ── Load theme from themes.json ─────────────────────────────────
THEME_NAME="${CLAUDE_THEME:-github}"
THEMES_FILE="/home/claude/themes.json"

THEME=$(python3 << PYEOF
import json, sys
try:
    with open("${THEMES_FILE}") as f:
        themes = json.load(f)
    name = "${THEME_NAME}"
    theme = themes.get(name)
    if not theme:
        print(f"Unknown theme: {name}", file=sys.stderr)
        avail = ", ".join(sorted(themes.keys()))
        print(f"Available: {avail}", file=sys.stderr)
        print(f"Defaulting to 'github'", file=sys.stderr)
        theme = themes["github"]
    print(json.dumps(theme))
except Exception as e:
    print(f"Theme error: {e}", file=sys.stderr)
    print('{"foreground":"#3e3e3e","background":"#f4f4f4","cursor":"#3f3f3f","black":"#3e3e3e","brightBlack":"#666666","red":"#970b16","brightRed":"#de0000","green":"#07962a","brightGreen":"#87d5a2","yellow":"#f8eec7","brightYellow":"#f1d007","blue":"#003e8a","brightBlue":"#2e6cba","magenta":"#e94691","brightMagenta":"#ffa29f","cyan":"#89d1ec","brightCyan":"#1cfafe","white":"#ffffff","brightWhite":"#ffffff"}')
PYEOF
)

# Start ttyd on internal port (gateway proxies to it)
export TTYD_PORT=8081
ttyd \
    --port "$TTYD_PORT" \
    --writable \
    --url-arg \
    --client-option "titleFixed=Claude Terminal" \
    --client-option "fontSize=13" \
    --client-option "fontFamily=JetBrains Mono,Fira Code,Cascadia Code,monospace" \
    --client-option "theme=${THEME}" \
    /home/claude/start-session.sh &

# Wait for ttyd to start
sleep 1

# Start Node.js gateway on public port (serves landing page + proxies to ttyd)
exec node /home/claude/gateway.js
