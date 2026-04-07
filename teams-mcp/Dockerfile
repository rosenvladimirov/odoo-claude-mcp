FROM ghcr.io/inditextech/mcp-teams-server:latest AS teams-base

FROM node:22-bookworm-slim

LABEL maintainer="BL Consulting"
LABEL description="Microsoft Teams MCP server (stdio→SSE via supergateway)"

# Install supergateway
RUN npm install -g supergateway

# Install Python + uv (needed by mcp-teams-server)
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Copy the teams server from the base image
COPY --from=teams-base /app /app

WORKDIR /app

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages -r <(cat <<'REQS'
microsoft-agents-authentication-msal>=0.8.0
microsoft-agents-activity>=0.8.0
microsoft-agents-hosting-core>=0.8.0
microsoft-agents-hosting-teams>=0.8.0
microsoft-agents-hosting-aiohttp>=0.8.0
python-dotenv>=1.2.1
mcp[cli]==1.26.0
msgraph-sdk>=1.55.0
setuptools>=82.0.1
azure-identity
REQS
) 2>/dev/null || pip3 install --no-cache-dir --break-system-packages \
    "microsoft-agents-authentication-msal>=0.8.0" \
    "microsoft-agents-activity>=0.8.0" \
    "microsoft-agents-hosting-core>=0.8.0" \
    "microsoft-agents-hosting-teams>=0.8.0" \
    "microsoft-agents-hosting-aiohttp>=0.8.0" \
    "python-dotenv>=1.2.1" \
    "mcp[cli]==1.26.0" \
    "msgraph-sdk>=1.55.0" \
    "setuptools>=82.0.1" \
    "azure-identity"

ENV MCP_PORT=8087

EXPOSE ${MCP_PORT}

CMD ["sh", "-c", "supergateway --stdio 'python3 -m mcp_teams_server -t stdio' --port ${MCP_PORT}"]
