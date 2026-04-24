# Cloudflare MCP plugin

MCP server за управление на Cloudflare Tunnel ingress + DNS records,
обвит с `supergateway` (stdio → SSE) за включване като proxy plugin
в `mcp-odoo-rpc` main stack.

## Tools

### Zones / DNS
- `cf_zones_list(name?)`
- `cf_dns_list(zone_id, name?, type?)`
- `cf_dns_create(zone_id, type, name, content, proxied, ttl, comment?)`
- `cf_dns_update(zone_id, record_id, **fields)`
- `cf_dns_delete(zone_id, record_id)`

### Tunnels
- `cf_tunnel_list(account_id?, include_deleted)`
- `cf_tunnel_config_get(tunnel_id, account_id?)`
- `cf_tunnel_config_put(tunnel_id, ingress[], account_id?, warp_routing)`
- `cf_tunnel_route_add(tunnel_id, zone_id, hostname, service, account_id?, create_dns, comment?)`
  — high-level: merges ingress + creates CNAME в един call
- `cf_tunnel_route_remove(tunnel_id, zone_id, hostname, account_id?, delete_dns)`
  — инверсен: махва ingress + CNAME

## Env
- `CF_API_TOKEN` — required. Permissions: `Account:Cloudflare Tunnel:Edit` + `Zone:DNS:Edit` + `Zone:Zone:Read`.
- `CF_ACCOUNT_ID` — optional default (може да се подаде per-call)
- `MCP_PORT` — default 8091

## Регистрация в main stack

`proxy_services.json`:
```json
"cloudflare": {"transport": "sse", "url": "http://cloudflare-mcp:8091/sse"}
```
