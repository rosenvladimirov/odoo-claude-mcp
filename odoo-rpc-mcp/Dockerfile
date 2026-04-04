FROM python:3.13-slim

LABEL maintainer="BL Consulting <rosen@blconsulting.bg>"
LABEL description="Odoo RPC MCP Server (XML-RPC/JSON-RPC)"

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --chmod=644 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=644 server.py .
COPY --chmod=644 google_service.py .

RUN useradd -m -s /bin/bash mcp && \
    mkdir -p /data && chown mcp:mcp /data
USER mcp

EXPOSE 8084

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=5s \
    CMD curl -f http://localhost:8084/health || exit 1

ENTRYPOINT ["python", "server.py"]
