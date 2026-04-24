FROM node:22-bookworm-slim

LABEL maintainer="BL Consulting"
LABEL description="Backup MCP server (stdio→SSE via supergateway) — stock snapshots, DB dumps, future S3 sync"

# Install supergateway + official filesystem MCP server (reused as storage backend for now)
RUN npm install -g supergateway @modelcontextprotocol/server-filesystem

# Do NOT pre-create /backups in the image. If the mount target exists at build
# time, the Docker daemon will try to chmod the newly-attached volume to inherit
# the image dir's perms — which s3fs (via the marcelo-ochoa plugin) rejects with
# "input/output error". Let the volume driver be the sole creator of /backups
# at container start. We run as root at /; the filesystem MCP server doesn't
# need privilege; the s3fs plugin handles uid/gid via its opts.

WORKDIR /

ENV MCP_PORT=8092

EXPOSE ${MCP_PORT}

CMD ["sh", "-c", "supergateway --stdio 'npx -y @modelcontextprotocol/server-filesystem /backups' --port ${MCP_PORT}"]
