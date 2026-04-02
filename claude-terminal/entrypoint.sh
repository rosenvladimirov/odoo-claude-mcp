#!/bin/bash

LIGHT='{"background":"#ffffff","foreground":"#1e1e1e","cursor":"#1e1e1e","cursorAccent":"#ffffff","selectionBackground":"rgba(0,0,0,0.15)","black":"#1e1e1e","red":"#c0392b","green":"#27ae60","yellow":"#f39c12","blue":"#2980b9","magenta":"#8e44ad","cyan":"#16a085","white":"#bdc3c7","brightBlack":"#636e72","brightRed":"#e74c3c","brightGreen":"#2ecc71","brightYellow":"#f1c40f","brightBlue":"#3498db","brightMagenta":"#9b59b6","brightCyan":"#1abc9c","brightWhite":"#ecf0f1"}'

DARK='{"background":"#1e1e2e","foreground":"#cdd6f4","cursor":"#f5e0dc","cursorAccent":"#1e1e2e","selectionBackground":"rgba(205,214,244,0.2)","black":"#45475a","red":"#f38ba8","green":"#a6e3a1","yellow":"#f9e2af","blue":"#89b4fa","magenta":"#cba6f7","cyan":"#94e2d5","white":"#bac2de","brightBlack":"#585b70","brightRed":"#f38ba8","brightGreen":"#a6e3a1","brightYellow":"#f9e2af","brightBlue":"#89b4fa","brightMagenta":"#cba6f7","brightCyan":"#94e2d5","brightWhite":"#a6adc8"}'

if [ "${CLAUDE_THEME:-light}" = "dark" ]; then
    THEME="$DARK"
else
    THEME="$LIGHT"
fi

exec ttyd \
    --port 8080 \
    --writable \
    --url-arg \
    --client-option "titleFixed=Claude Terminal" \
    --client-option "fontSize=13" \
    --client-option "fontFamily=JetBrains Mono,Fira Code,Cascadia Code,monospace" \
    --client-option "theme=${THEME}" \
    /home/claude/start-session.sh
