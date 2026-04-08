FROM node:22-bookworm-slim

LABEL maintainer="BL Consulting"
LABEL description="Filesystem MCP server (stdio→SSE via supergateway)"

# Install supergateway + official filesystem MCP server
RUN npm install -g supergateway @modelcontextprotocol/server-filesystem

# Create data directory
RUN mkdir -p /repos && chown node:node /repos

USER node
WORKDIR /repos

ENV MCP_PORT=8088

EXPOSE ${MCP_PORT}

# /repos is the allowed root directory — all file operations are sandboxed here
CMD ["sh", "-c", "supergateway --stdio 'npx -y @modelcontextprotocol/server-filesystem /repos' --port ${MCP_PORT}"]
