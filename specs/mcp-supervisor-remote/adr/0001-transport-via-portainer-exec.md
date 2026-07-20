# ADR-0001: Дистанционен контрол на supervisor.py — транспорт през Portainer docker exec

- **Статус:** Accepted (транспорт) — 2026-07-17
- **Автори:** Rosen Vladimirov, Клаудчо
- **Свързани:** `specs/mcp-vpn-tool/adr/0001-*` (VPN трак); дизайн workflow за supervisor remote control (17.07); образ `vladimirovrosen/odoo:supervisor-19.0-slim`

## Контекст

`supervisor.py` е one-shot orchestrator за Odoo addons (git pull, symlinks, OCA/EE, pip) → дистанционното му управление е **remote-root-RCE surface** и трябва да е зад силна автентикация.

Дизайн workflow (4 подхода + adversarial security) отхвърли bespoke websocket (raw/FastAPI/Centrifugo — всички **weak**: нов интернет-достъпен RCE endpoint на споделен docker bridge, заобикаля Cloudflare Access; преизобретяват auth/streaming). Единствено MCP-tool пътят беше **viable**.

Ограничение (Rosen): **няма изходящ SSH** — операторът е „затворен в Portainer". Значи `ssh_execute` не е универсален транспорт. Portainer е валидиран на живо (2.33.2, **Docker proxy exec достъпен**, `mcp-portainer` работи).

## Решение

**Транспортът за supervisor контрол = Portainer Docker-proxy `exec` в supervisor slim контейнера (Path A).** Избрано от Rosen на 17.07.

- **НЕ през VPN хъба (Path B).** Хъбът (ADR mcp-vpn-tool) е за стакове без Portainer достъп или когато трябва L3 — не е задължителен за supervisor.
- **НЕ SSH** (недостъпен).
- Реализация: admin tool в `odoo-rpc` MCP, който вика Portainer docker exec (или чрез съществуващия `portainer-mcp`), с преизползване на `identify`/`mcp_elevate`/**TOTP** gate.
- **v1 обхват:** пълен (вкл. force/oca/ee/init), но **код-изпълняващите режими зад задължителен TOTP** (решено по-рано).

## Сигурност (задължителни, от workflow-а)

fail-closed mode enum (unknown→reject) ✅ реализирано; НИКАКВИ caller-подадени conf/branch/flags (пекат се host-side от per-alias блок) ✅; **digest-pinned** toolbox образ (`image@sha256`, не tag) — препоръчано в config; audit ledger ✅ (`supervisor_deploy_ops.jsonl`); odoo-rpc порт да не е на споделен bridge (само Cloudflare tunnel).

### 🔧 Корекция (2026-07-17): TOTP-per-action НЕ съществува
Кодовата база **няма** per-action TOTP примитив — `identify_verify_totp` е само втори фактор при name-identify (връзва принципал към сесия), не challenge за отделно извикване. Затова първоначалното „TOTP gate за разрушителните" **не е реализуемо as-is**. Наличната предпазна примитива (както `module_deploy`):
- **admin-principal gate** (server.py, автоматично щом имената са в `ADMIN_TOOL_NAMES`),
- **DRY-RUN by default** за разрушителните (oca/ee/force/init) — само план, освен `dry_run=false` + `MCP_SUPERVISOR_DRY_RUN=0`,
- **`read_only` флаг** на alias-а блокира разрушителните режими,
- **USER-role blocked** (`tool_security.DEFAULT_USER_BLOCKED_TOOLS`).

→ Решение за Rosen: приемаме DRY-RUN-default като заместител на TOTP, ИЛИ добавяме нов елевационен/confirm примитив преди да включим разрушителните на живо. `serialization lock` за конкурентни run-ове — TODO (v1 status е безопасен и без него).

## Статус на имплементацията (2026-07-17)
- `odoo-rpc-mcp/supervisor_deploy.py` написан (tools: `supervisor_status` read-only, `supervisor_run` DRY-RUN-default, `supervisor_history`) + 5 регистрации в `server.py`/`tool_security.py` — **compile-clean**.
- **Транспортът live-верифициран** срещу `odoo-dev-server` Portainer (pull→create→start→wait exit=0→logs→remove).
- ⏳ НЕ деплойнат (mcp-odoo-rpc:3.3.1 върти стар код — чака consent за rebuild/redeploy).
- ⏳ Нужен per-alias `supervisor` блок в connections.json (image, conf_path, binds, endpoint_id) за реален `--github-status` срещу стак.

## Последствия

**Плюсове:** най-прост; без зависимост от вдигнат VPN; преизползва валидиран Portainer + MCP auth. **Минуси:** вързан за наличността на Portainer; supervisor контейнерът трябва да върви (sidecar) или да се пуска on-demand в целевия стак.

## Отворени въпроси

1. 39-те tools на `portainer-mcp` излагат ли `container exec`, или добавяме `supervisor_*` tool в odoo-rpc, който вика Portainer docker exec директно?
2. supervisor контейнер = **running sidecar** (exec on-demand) или **on-demand run / `--init-container`** при redeploy?
3. Първи целеви стакове.
