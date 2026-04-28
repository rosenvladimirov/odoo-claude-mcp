FROM python:3.13-slim

LABEL maintainer="Rosen Vladimirov <vladimirov.rosen@gmail.com>"
LABEL description="Odoo RPC MCP Server (XML-RPC/JSON-RPC)"

# Tag convention (Docker Hub / registry):
#   :latest  → :stable  → last 2.x build  (production across all tiers)
#   :2.x.y   → semver of end-user track (maintained on branch 2.0)
#              Current: 2.19.0 — AI OCR gap-closure set (P0+P1+P2+P3 Trust
#              Foundation, Budget, RAG, BG validators, reviewer dashboard)
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
COPY --chmod=644 admin_ui.py .
COPY --chmod=644 admin_backup.py .
COPY --chmod=644 admin_filestore.py .
COPY --chmod=644 metrics.py .
COPY --chmod=644 bg_validators.py .
COPY --chmod=644 pdf_sanitizer.py .
COPY --chmod=644 tenant_router.py .
COPY --chmod=644 sql_classifier.py .
COPY --chmod=644 sql_executor.py .
COPY --chmod=644 tool_security.py .
COPY --chmod=644 elevation.py .
COPY --chmod=644 api_key_manager.py .
COPY --chmod=644 provisioning_engine.py .
COPY --chmod=644 provisioning_api.py .
COPY --chmod=644 cloudflare_provisioning.py .
COPY --chmod=644 client_stack.template.yml .

RUN useradd -m -s /bin/bash mcp && \
    mkdir -p /data && chown mcp:mcp /data
USER mcp

EXPOSE 8084

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=5s \
    CMD curl -f http://localhost:8084/health || exit 1

ENTRYPOINT ["python", "server.py"]
