FROM node:22-bookworm-slim

LABEL maintainer="Rosen Vladimirov <vladimirov.rosen@gmail.com>"
LABEL description="Per-session ephemeral Claude CLI container (Phase 2.2 isolation)"
LABEL phase="2.2"

# Minimal runtime: claude CLI + python3 (for hash + theme + .mcp.json render).
# No ttyd inside — the gateway pipes WebSocket stdio directly into the
# entrypoint. No build tooling kept in the final layer.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl python3 tini && \
    rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code && \
    npm cache clean --force

# Use uid 1000 (claude); home is bind-mounted by gateway from
# /shared/users/${PROFILE} — image directory is just a fallback.
RUN usermod -d /home/claude -l claude -s /bin/bash node && \
    groupmod -n claude node && \
    mv /home/node /home/claude && \
    chown -R claude:claude /home/claude

USER claude
WORKDIR /home/claude

ENV TERM=xterm-256color
ENV HOME=/home/claude
ENV USER=claude
ENV SHELL=/bin/bash
ENV CLAUDE_CODE_SKIP_REFERRAL=1

COPY --chown=claude entrypoint.sh /home/claude/entrypoint.sh
RUN chmod +x /home/claude/entrypoint.sh

# tini for proper signal handling — gateway sends SIGTERM on socket close
ENTRYPOINT ["/usr/bin/tini", "--", "/home/claude/entrypoint.sh"]
