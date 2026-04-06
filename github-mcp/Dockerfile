FROM ghcr.io/github/github-mcp-server:latest AS src

FROM odoo-mcp-portainer-mcp:latest

COPY --from=src /server/github-mcp-server /usr/local/bin/github-mcp-server

ENV MCP_PORT=8086
EXPOSE ${MCP_PORT}

CMD ["sh", "-c", "supergateway --stdio 'github-mcp-server stdio' --port ${MCP_PORT}"]
