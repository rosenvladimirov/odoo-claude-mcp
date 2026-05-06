FROM node:22-bookworm-slim AS ttyd-builder

# Build ttyd in a discardable layer — final image gets only the binary.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config git ca-certificates \
        libjson-c-dev libwebsockets-dev libuv1-dev zlib1g-dev libssl-dev && \
    git clone --depth 1 https://github.com/tsl0922/ttyd.git /tmp/ttyd && \
    cd /tmp/ttyd && mkdir build && cd build && \
    cmake .. && make -j"$(nproc)" && \
    cp ttyd /usr/local/bin/ttyd

# ── final image ────────────────────────────────────────────────
FROM node:22-bookworm-slim

LABEL maintainer="Rosen Vladimirov <vladimirov.rosen@gmail.com>"
LABEL description="Per-session ephemeral Claude CLI container (Phase 2.2 isolation)"
LABEL phase="2.2"

# Runtime deps only — no build tooling kept.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl python3 tini \
        libjson-c5 libwebsockets17 libwebsockets-evlib-uv \
        libuv1 zlib1g libssl3 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ttyd-builder /usr/local/bin/ttyd /usr/local/bin/ttyd

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code && \
    npm cache clean --force

# uid 1000 (claude); $HOME is bind-mounted by gateway from
# /srv/claude-terminal-shared/users/${PROFILE} → /home/claude.
RUN usermod -d /home/claude -l claude -s /bin/bash node && \
    groupmod -n claude node && \
    mv /home/node /home/claude && \
    chown -R claude:claude /home/claude

# Scripts live in /usr/local/bin so they survive the gateway's bind mount
# of the persistent slice over /home/claude (which would otherwise hide
# anything copied into $HOME at build time).
COPY entrypoint.sh /usr/local/bin/claude-session-entrypoint
COPY session-shell.sh /usr/local/bin/claude-session-shell
RUN chmod +x /usr/local/bin/claude-session-entrypoint /usr/local/bin/claude-session-shell

USER claude
WORKDIR /home/claude

ENV TERM=xterm-256color
ENV HOME=/home/claude
ENV USER=claude
ENV SHELL=/bin/bash
ENV CLAUDE_CODE_SKIP_REFERRAL=1

# ttyd internal port — gateway resolves session-${id}:8081 via Docker DNS
ENV TTYD_PORT=8081
EXPOSE 8081

# tini handles SIGTERM propagation when gateway closes the WebSocket
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/claude-session-entrypoint"]
