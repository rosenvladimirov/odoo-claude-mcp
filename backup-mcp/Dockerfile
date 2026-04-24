FROM node:22-bookworm-slim

LABEL maintainer="BL Consulting"
LABEL description="Backup MCP server (stdio→SSE via supergateway) — stock snapshots, DB dumps, future S3 sync"

# Install supergateway + official filesystem MCP server (reused as storage backend for now)
RUN npm install -g supergateway @modelcontextprotocol/server-filesystem

# Create backups directory (shared volume mount point)
RUN mkdir -p /backups && chown node:node /backups

USER node
WORKDIR /backups

ENV MCP_PORT=8092

EXPOSE ${MCP_PORT}

# /backups is the allowed root — shared with odoo-rpc-mcp via mcp-backups volume.
# Stock tools write JSON snapshots here before destructive ops; this plugin exposes
# read/list/delete via MCP tools. Future: GUI, S3 Contabo sync, archive rotation.
CMD ["sh", "-c", "supergateway --stdio 'npx -y @modelcontextprotocol/server-filesystem /backups' --port ${MCP_PORT}"]
