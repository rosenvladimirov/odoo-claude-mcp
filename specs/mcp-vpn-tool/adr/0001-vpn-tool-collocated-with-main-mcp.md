# ADR-0001: VPN tool в MCP — co-located с main MCP хъб, sidecar-driven

- **Статус:** Accepted (топология) — 2026-07-17; rollout цели отложени
- **Автори:** Rosen Vladimirov, Клаудчо
- **Свързани:** Portainer-supervisor трак (`specs/mcp-supervisor-remote/`, предстои); `k3s/base/portainer-mcp.yaml`; `odoo-rpc-mcp/`

## Контекст

Основният оперативен проблем: **операторът е „затворен в Portainer" — няма изходящ SSH/директен мрежов достъп** до повечето стакове. Единственият универсален management достъп е Portainer API (валидиран на живо — 2.33.2, Docker proxy достъпен, `mcp-portainer` вече работи). Това покрива **management-plane** (docker exec, stack update), но не и **data-plane** L3 достъп до услуги вътре в изолираните мрежи.

За L3 достъпимост вече съществува **работещ WireGuard хъб**, co-located с main MCP (директиватa на Rosen: „където е main MCP, там е и ЖПН-а"). Валидирано на живо на `odoo-dev-server` (публичен IP на хъба **62.171.156.220**):

- `linuxserver/wireguard` в **server/hub режим** (`CAP_NET_ADMIN`, `CAP_SYS_MODULE`, `net.ipv4.ip_forward=1`)
- Публичен endpoint **62.171.156.220:51820/udp**; VPN подмрежа **10.10.10.0/24**
- `ALLOWEDIPS = 10.10.10.0/24, 192.168.1.0/24, 192.168.2.0/24` → site-to-site към клиентски LAN-и
- 5 съществуващи peer-а: `rosen, stamatis, bgrouter, grrouter, cyrouter` (вкл. рутери BG/GR/CY)
- На същия хост вървят: `mcp-odoo-rpc:3.3.1`, `mcp-portainer`, `cloudflare` (tunnel)

Т.е. няма нужда от нова инфраструктура — надграждаме съществуващ хъб.

## Решение

Добавяме **`vpn_*` tool група в главния `odoo-rpc` MCP сървър**, която **управлява co-located WireGuard хъб контейнера като sidecar**, а НЕ придобива сама мрежови привилегии.

1. **Топология:** main-MCP хостът Е WireGuard хъбът. Изолираните стакове стартират WG **client** контейнер (`linuxserver/wireguard`, същият образ), който **набира навън** към `62.171.156.220:51820` → влиза в `10.10.10.0/24`. Никакъв нов inbound порт на клиентския стак.

2. **Bootstrap през Portainer (решава chicken-and-egg):** щом стигаме Portainer на изолиран стак, **деплойваме WG-client контейнера ПРЕЗ Portainer**; след handshake имаме и директен L3. Portainer bootstrap-ва VPN-а.

3. **Least privilege:** MCP контейнерът **не** взима `NET_ADMIN`/`/dev/net/tun`. Управлява хъба през docker exec (локален socket / Portainer) с `wg` / `wg set` / `wg-quick save`. Тунелът остава в wg контейнера.

4. **Peer provisioning — live, без recreate:** динамично `wg set peer <pub> allowed-ips <ip/32>` + `wg-quick save` (или запис в `/config`), за да НЕ рестартираме хъба (рестарт къса всички тунели). `PEERS` env recreate се ползва само за первоначален seed.

### Tool surface (`vpn_*` в odoo-rpc MCP)

| Tool | Действие | Gate |
|---|---|---|
| `vpn_status` | интерфейс up? peers, last handshake, allowed-ips, transfer | read (USER-blocked, admin) |
| `vpn_reachability(host[,port])` | ping/tcp тест след вдигане | read |
| `vpn_peer_list` | изброяване на peer-и + метадата | read |
| `vpn_peer_add(name, pubkey, allowed_ips)` | добавя peer live | **admin + TOTP** |
| `vpn_peer_remove(name)` | маха peer live | **admin + TOTP** |
| `vpn_config_issue(name, allowed_ips)` | генерира клиентски конфиг (ключове → `secrets_register`) | **admin + TOTP** |

## Сигурност (задължителни изисквания)

- **Мрежов достъп = мощно** → всеки write (`peer_add/remove`, `config_issue`) зад **admin principal + `identify_verify_totp`** вътре в gate-а; не caller-подаден confirm токен.
- **`AllowedIPs` scope-нат per peer** (напр. `/32`), НЕ рутирай цялата подмрежа към нов peer без нужда.
- **Ключове/конфиги само в `secrets_registry`**, per-stack; никога не се ехват обратно по wire; ротация.
- **Няма shell injection:** валидирай `name` (строг charset), `pubkey` (base64 44 знака), `allowed_ips` (CIDR list) преди интерполация в exec команда.
- **Audit ledger** на всяка vpn операция (`{principal, action, peer, allowed_ips, ts}`, 0600) — както fleet/module_deploy.
- **Не** излагай хъб порт извън 51820/udp; MCP порт достъпен само през Cloudflare tunnel (не на споделен bridge).
- **Kill-switch:** `vpn_peer_remove` = незабавно отнемане на достъп; документирай, че live save персистира.

## Фазиране

- **P0 (read-only):** `vpn_status`, `vpn_peer_list`, `vpn_reachability` срещу вече работещия хъб. Доказва auth/audit пътя.
- **P1:** `vpn_peer_add/remove` live (admin+TOTP).
- **P2:** `vpn_config_issue` + `secrets_registry` интеграция + Portainer-bootstrap рецепта за WG-client в изолиран стак.

## Последствия

**Плюсове:** нула нова инфра (хъбът + Portainer вече работят); MCP остава непривилегирован; L3 достъп до изолирани стакове/LAN-и; допълва Portainer (management-plane) с data-plane.

**Минуси / рискове:** хъбът става single point of access (компрометиран admin принципал = мрежов достъп до всички peer-и) → TOTP + audit са критични; live `wg` промени трябва да се персистират, иначе се губят при рестарт; co-located дизайн връзва VPN към наличността на main-MCP хоста.

## Решения (2026-07-17)

1. **Каноничен хъб = main сървърът** (WG хъбът на main-MCP хоста, `62.171.156.220:51820`). Без отделен централен хъб. ✅
2. Първи целеви стакове — **отложено** (Rosen: „чакай"). Rollout се планира по-късно.
3. MCP собствен peer vs само-провизиране — **неопределено**, остава отворено до P1.

## Оперативно правило (Rosen, 2026-07-17)
**При ВСЯКА промяна, засягаща MCP** (`odoo-mcp-v3`, MCP tools, MCP деплой) — да се **напомня за този VPN трак**, за да не се забрави докато тече друга MCP работа. Записано като always-load памет `feedback_remind_vpn_track_on_mcp_changes`.
