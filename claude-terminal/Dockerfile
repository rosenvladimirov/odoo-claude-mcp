FROM node:22-bookworm-slim

LABEL maintainer="BL Consulting"
LABEL description="Claude Code CLI with ttyd web terminal"

# Build ttyd from source
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config git ca-certificates \
        libjson-c-dev libwebsockets-dev libuv1-dev zlib1g-dev libssl-dev \
        curl tmux python3 && \
    git clone --depth 1 https://github.com/tsl0922/ttyd.git /tmp/ttyd && \
    cd /tmp/ttyd && mkdir build && cd build && \
    cmake .. && make -j$(nproc) && cp ttyd /usr/local/bin/ && \
    cd / && rm -rf /tmp/ttyd && \
    apt-get purge -y build-essential cmake pkg-config && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Use node user (uid 1000) matching host
RUN usermod -d /home/claude -l claude -s /bin/bash node && \
    mv /home/node /home/claude

USER claude
WORKDIR /home/claude

ENV TERM=xterm-256color
ENV HOME=/home/claude
ENV USER=claude
ENV SHELL=/bin/bash
ENV CLAUDE_CODE_SKIP_REFERRAL=1

EXPOSE 8080

COPY --chown=claude entrypoint.sh /home/claude/entrypoint.sh
COPY --chown=claude start-session.sh /home/claude/start-session.sh
RUN chmod +x /home/claude/entrypoint.sh /home/claude/start-session.sh

ENTRYPOINT ["/home/claude/entrypoint.sh"]
