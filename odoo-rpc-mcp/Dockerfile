FROM python:3.13-slim

LABEL maintainer="BL Consulting <rosen@blconsulting.bg>"
LABEL description="Odoo RPC MCP Server (XML-RPC/JSON-RPC)"

# Tag convention (Docker Hub / registry):
#   :latest  → :stable  → last 2.x build  (production across all tiers)
#   :2.x.y   → semver of end-user track (maintained on branch 2.0)
#              Current: 2.10.0 — translate + website snippet tools
#   :next    → last 3.x build (integrator platform — preview, not production)
#   :3.x.y   → semver of integrator track (maintained on branch 3.0)
# Branch 2.0 is the default repo branch and source of :latest/:stable.

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --chmod=644 requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chmod=644 server.py .
COPY --chmod=644 google_service.py .
COPY --chmod=644 telegram_service.py .
COPY --chmod=644 ai_usage_log.py .
COPY --chmod=644 ai_vision_service.py .
COPY --chmod=644 ai_invoice_engine.py .

RUN useradd -m -s /bin/bash mcp && \
    mkdir -p /data && chown mcp:mcp /data
USER mcp

EXPOSE 8084

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=5s \
    CMD curl -f http://localhost:8084/health || exit 1

ENTRYPOINT ["python", "server.py"]
