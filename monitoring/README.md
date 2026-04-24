# mcp-monitoring

Standalone Prometheus + Grafana stack for the poligroup MCP fleet. Runs
as its own Docker Compose project (`/opt/mcp-monitoring/`), joins the
shared `odoo-claude-mcp_backend` network, and scrapes the `/metrics`
endpoint of every MCP stack.

## Components

| Service | Purpose |
|---|---|
| `prometheus` | Scrapes metrics, 90d retention by default |
| `grafana` | Dashboards, accessible at `https://grafana.mcpworks.net` |
| `node-exporter` | poligroup host metrics (CPU, RAM, disk, net) |

## Scrape targets (internal DNS via `odoo-claude-mcp_backend`)

- `mcp-odoo-rpc:8084` — main MCP
- `odoo-rpc-mcp-<ID>:809x` — 5 client stacks
- `node-exporter-monitoring:9100` — host

## Bootstrap

On poligroup:

```bash
mkdir -p /opt/mcp-monitoring
rsync -av --exclude .git monitoring/ root@poligroup:/opt/mcp-monitoring/
ssh root@poligroup '
cat > /opt/mcp-monitoring/stack.env <<EOF
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=<random-strong-password>
PROM_RETENTION=90d
EOF
cd /opt/mcp-monitoring && docker compose --env-file stack.env up -d
'
```

## Cloudflare tunnel

Add this hostname to the existing `mcpworks.net` tunnel
(`fcc57c3e-1c72-4428-ab1e-71764ed43a48`):

- `grafana.mcpworks.net` → `http://grafana-monitoring:3000`

CNAME: `grafana.mcpworks.net` → `<tunnel-id>.cfargotunnel.com`.

## Health checks

```bash
# Prometheus scrape status
curl -s http://prometheus-monitoring:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, tenant: .labels.tenant, health: .health}'

# Grafana ready
curl -s https://grafana.mcpworks.net/api/health
```

## Dashboards (auto-provisioned)

- **MCP Overview** — fleet-wide: sessions, tool calls, error rate, per-tenant
  breakdowns, top tools, HTTP traffic, backup writes, proxy discoveries.
- **Host — poligroup** — CPU, memory, disk, network, load, IO.

## Security notes

- Grafana is reachable from the public internet via the Cloudflare tunnel.
  Use a strong `GRAFANA_ADMIN_PASSWORD` and optionally enable Cloudflare
  Access for SSO/MFA in front of `grafana.mcpworks.net`.
- Prometheus has no public route — it talks to MCP `/metrics` only inside
  the `odoo-claude-mcp_backend` network.
- Main's `:8084` is currently port-exposed; `/metrics` is therefore also
  reachable publicly. Scraping works via internal DNS, but consider a
  Cloudflare WAF rule to block `/metrics` externally.
