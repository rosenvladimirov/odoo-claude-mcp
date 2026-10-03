# Изпълнителят на Клаудчо при Docker (ADR mcp-supervisor-remote/0003, временно до клъстера):
# тегли от Odoo стъпките container/teardown и пуска стековете klaudcho-* с docker compose.
# Изграждане от корена на odoo-mcp-v3:  docker build -f odoo-supervisor-agent/Dockerfile -t odoo-supervisor-agent:<версия> .
FROM docker:27-cli
RUN apk add --no-cache python3 tini
WORKDIR /app
COPY odoo-rpc-mcp/supervisor_dispatch.py odoo-rpc-mcp/klaudcho_stack.template.yml /app/
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/sbin/tini", "--", "python3", "/app/supervisor_dispatch.py"]
