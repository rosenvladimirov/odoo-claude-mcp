FROM node:22-bookworm-slim

LABEL maintainer="Rosen Vladimirov <vladimirov.rosen@gmail.com>"
LABEL description="Claude Terminal gateway — per-session container dispatcher (Phase 2.2)"
LABEL phase="2.2"

# Tini for signal forwarding so SIGTERM tears down lingering session
# containers cleanly. ca-certificates needed for MCP /api calls.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force

COPY gateway.js ./
COPY landing.html ./
COPY tools.html ./

# Listen port — proxied behind Traefik / Cloudflare in production
EXPOSE 8080

# Gateway runs as root so it can talk to /var/run/docker.sock; the
# spawned session containers are NOT root (uid 1000 inside).
ENV NODE_ENV=production
ENV GATEWAY_PORT=8080

ENTRYPOINT ["/usr/bin/tini", "--", "node", "/app/gateway.js"]
