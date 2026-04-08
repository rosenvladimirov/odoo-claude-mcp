FROM python:3.12-slim-bookworm

LABEL maintainer="BL Consulting"
LABEL description="OCA MCP server — OCA addon repository management (stdio→SSE via supergateway)"

# Install system deps (git, rsync for deploy, node for supergateway)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git rsync ca-certificates curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g supergateway && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Copy server
COPY server.py /app/server.py
WORKDIR /app

# Directories for both modes
RUN mkdir -p /opt/odoo /repos && \
    useradd -m -s /bin/bash mcp && \
    chown -R mcp:mcp /opt/odoo /repos /app

USER mcp

ENV MCP_PORT=8089
ENV ODOO_BRANCH=19.0
ENV OCA_DIRECT_DIR=/opt/odoo
ENV OCA_BUFFER_DIR=/repos

EXPOSE ${MCP_PORT}

CMD ["sh", "-c", "supergateway --stdio 'python3 /app/server.py' --port ${MCP_PORT}"]
