# v3 Transparent Proxy към remote v2.x

## Цел

v3 = developer/integrator gateway, който прозрачно проксира **всички tools** на N
дистанционни v2.x MCP инстанции, за да може един Claude Code прозорец да работи
върху main + всички клиентски стака без преконфигуриране.

**v3 не добавя нова логика** върху сегашната v2 архитектура — придържа се към
`mcp_terminal_get_config` за всичко tenant-specific.

## Защо няма нужда от нов код

`odoo-rpc-mcp` от v2.24.0 нататък вече има generic HTTP proxy с:
- `streamablehttp_client` за external HTTPS endpoints
- Bearer auth през `headers.Authorization`
- `${VAR}` env expansion в headers (`server.py:96`)
- Auto tool префикс `service__tool` (`server.py:155`)
- Discovery + retry + refresh

За v3 transparent proxy → **само конфиг + Bearer tokens**.

## Bootstrap workflow

### Стъпка 1 — еднократно SSH извличане на MCP_SECRET_TOKEN

Поради дизайна на v2.x, runtime токените живеят в **Portainer stack.env**
(не в Odoo settings, не във filestore). Затова bootstrap-ът е през SSH:

```bash
# Client стака на poligroup
ssh root@164.68.114.107 'for d in /opt/mcp-clients/*/; do
  id=$(basename "$d")
  echo "=== $id ==="
  grep -E "^(MCP_SECRET_TOKEN|MCP_OAUTH_CLIENT_ID|MCP_ADMIN_TOKEN|MCP_OAUTH_CLIENT_SECRET)=" "$d/stack.env" 2>/dev/null
done'

# Main + demo на 62.171.156.220
ssh root@62.171.156.220 'for f in /var/lib/docker/volumes/portainer_data/_data/compose/*/stack.env; do
  if grep -q MCP_SECRET_TOKEN "$f"; then echo "=== $f ==="; grep -E "^MCP_" "$f"; fi
done'
```

Резултатите → `~/Проекти/odoo/odoo-mcp-v3/.env` (gitignored).

### Стъпка 2 — конфигурация

```bash
cp proxy_services.v3.example.json /data/proxy_services.json
```

Един entry на remote v2.x:

```json
"teolino": {
  "transport": "http",
  "url": "https://mcp-208609891.mcpworks.net/mcp",
  "headers": { "Authorization": "Bearer ${MCP_TOKEN_208609891}" }
}
```

### Стъпка 3 — старт

При старт на v3 gateway-ът се свързва към всеки remote, discover-ва tools-ите
и ги регистрира с префикс. Пример:

```
main__odoo_search_read
teolino__odoo_search_read
c115353345__odoo_search_read
...
```

## Регистрирани targets (2026-04-28)

| Key | URL | Token env | Status |
|-----|-----|-----------|--------|
| `main` | mcp.odoo-shell.space | `MCP_TOKEN_MAIN` | v2.25.0 ok (REQUIRE_AUTH=0) |
| `c115353345` | mcp-115353345.mcpworks.net | `MCP_TOKEN_115353345` | v2.25.0 ok |
| `c115572378` | mcp-115572378.mcpworks.net | `MCP_TOKEN_115572378` | v2.25.0 ok (token празен) |
| `stage_tiva` | mcp-130931201.mcpworks.net | `MCP_TOKEN_130931201` | v2.25.0 ok |
| `c203709674` | mcp-203709674.mcpworks.net | `MCP_TOKEN_203709674` | v2.25.0 ok |
| `c207327615` | mcp-207327615.mcpworks.net | `MCP_TOKEN_207327615` | v2.25.0 ok (full OAuth) |
| `teolino` | mcp-208609891.mcpworks.net | `MCP_TOKEN_208609891` | v2.25.0 ok |

## Tenant config след bootstrap

След като v3 проксира към всички v2.x, tenant-specific config (Anthropic key,
Qdrant, Ollama, Claude Terminal URL) се извлича чрез:

```
<tenant>__mcp_terminal_get_config
```

Това е стандартен v2 tool, експониран чрез proxy. Не дублираме логиката в v3.

## Token cost

7 remote × ~80 native tools = ~560 prefixed tools в schema. При нужда:
- използвай `MCP_DISABLE_FEATURES` за филтриране
- или (по-късно) implement-вай dynamic tenant routing (виж "Roadmap")

## Verify

```bash
# Health на всички remote v2 endpoints
for host in mcp.odoo-shell.space mcp-{115353345,115572378,130931201,203709674,207327615,208609891}.mcpworks.net; do
  printf "%-40s " "$host"
  curl -fsS --max-time 8 https://$host/health | jq -r '"v\(.version) \(.status)"'
done

# v3 discovery log
docker logs mcp-odoo-rpc | grep "Proxy: discovered"
```

## Roadmap

- **Active tenant per session** — control plane tool `tenant_use(name)` + експонира
  само избрания client's tools (drastically reduces token cost)
- **`v3_register_v2_target(name, url, token)`** — runtime registration без restart
- **`v3_health_check_targets()`** — единен health probe
- **Failover** — при недостъпен remote, маркира tools като unavailable вместо да
  крашва discovery
