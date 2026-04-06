FROM node:22-bookworm-slim

LABEL maintainer="BL Consulting"
LABEL description="Portainer MCP server (stdio→SSE via supergateway)"

# Install supergateway (wraps stdio MCP as SSE/HTTP)
RUN npm install -g supergateway

# Download portainer-mcp binary
ARG PORTAINER_MCP_VERSION=v0.7.0
ARG TARGETARCH=amd64
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL "https://github.com/portainer/portainer-mcp/releases/download/${PORTAINER_MCP_VERSION}/portainer-mcp-${PORTAINER_MCP_VERSION}-linux-${TARGETARCH}.tar.gz" \
         -o /tmp/portainer-mcp.tar.gz && \
    tar -xzf /tmp/portainer-mcp.tar.gz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/portainer-mcp && \
    rm /tmp/portainer-mcp.tar.gz && \
    apt-get purge -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Go wrapper script that handles self-signed certs
RUN printf '#!/bin/sh\n\
if [ "$INSECURE_TLS" = "true" ]; then\n\
  export SSL_CERT_FILE=/dev/null\n\
  export GIT_SSL_NO_VERIFY=1\n\
fi\n\
exec portainer-mcp -server "$PORTAINER_URL" -token "$PORTAINER_TOKEN" \\\n\
  ${READ_ONLY:+-read-only} -disable-version-check\n' \
  > /usr/local/bin/run-portainer-mcp && chmod +x /usr/local/bin/run-portainer-mcp

ENV MCP_PORT=8085

EXPOSE ${MCP_PORT}

CMD ["sh", "-c", "supergateway --stdio '/usr/local/bin/run-portainer-mcp' --port ${MCP_PORT}"]
