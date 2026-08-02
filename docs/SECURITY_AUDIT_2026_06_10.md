# Security Audit — MCP сървър v2 (2.30.0, production) + v3 (dev)

> Дата: 2026-06-10. Метод: 5 паралелни read-only агента + ръчна верификация на код и `stack.env` на сървъра.
> Обхват: v2 `~/Проекти/odoo/odoo-mcp/odoo-rpc-mcp` (deployed на 8 poligroup стака), v3 `~/Проекти/odoo/odoo-mcp-v3/odoo-rpc-mcp` (dev, не deployed).
> ⚠️ Болшинството v2 находки са **заварени** (HTTP слой, предхождат session-миграцията). Strict session моделът сам по себе си издържа на челните атаки.

---

## V2 — production-активни (потвърдени в код + verifиран на сървъра)

### Разграничение: какво е НОВО vs ЗАВАРЕНО
- **Заварени** (предхождат 2.30.0): AUTH-DEBUG, OAuth self-issue, /api/user/connections IDOR, save_path write, /api/ai bypass, SSRF, TLS CERT_NONE, ssh/git injection, `_sanitize_name` `..`.
- **Изострени от strict модела**: `identify(name=...)` impersonation на non-unified път — заварен код, но сега `principal` е централният лост за достъп → по-висок импакт.
- **Нови, внесени от миграцията**: WAL sidecar файлове (`-wal`/`-shm`) не са 0600 (low).

### CRITICAL / HIGH (production)

| # | Находка | File:line | Статус на сървъра |
|---|---|---|---|
| **A** | **AUTH-DEBUG логва ВСИЧКИ headers** вкл. `Authorization: Bearer <Odoo api_key>` на INFO при всеки /mcp + OAuth request → живите Odoo ключове на всеки tenant текат в `docker logs`/Portainer | server.py:9551 | АКТИВЕН на 8/8 |
| **B** | **/api/user/connections IDOR**: GET по произволно `name` връща чужд `connections.json` вкл. **api_key plaintext**; POST презаписва чужди връзки. Нула owner-проверка (за разлика от register-connection). В protected_paths → нужен е валиден токен, но всеки tenant token минава | server.py:10331-10375 | АКТИВЕН на 8/8 |
| **C** | **OAuth self-issue на master токена**: `/oauth/register` е public; `oauth_client_secret` fallback-ва към `MCP_SECRET_TOKEN`; `/oauth/token client_credentials` връща `access_token=secret_token` → аноним получава master bearer | server.py:9458, 9472-9473, 9590-9680 | **АКТИВЕН на 131071078** (oauth==secret_token); митигиран където са различни. Трябва audit на 8/8 |
| **D** | **save_path произволен файлов запис**: `open(save_path,"wb")` без confinement в odoo_web_report + 6× public_access_* → запис навсякъде (authorized_keys, cron, server.py) → RCE | server.py:6688-6689, 6865, 6930, 6948, 6979, +още | АКТИВЕН на 8/8 |
| **E** | **/api/ai/extract-raw без auth за "internal"**: `_is_internal = ip.startswith("172.")` → CF трафик през docker bridge има source 172.x → auth се пропуска → аноним харчи ANTHROPIC_API_KEY бюджета | server.py:10377-10390, 10477-10489 | runtime-зависим (source IP), дизайнът е грешен |
| **F** | **SSRF в auth резолюцията**: `get_caller_odoo_user` прави XMLRPC към произволен `X-Odoo-Url` ПРЕДИ резолюция; `ALLOWED_ODOO_URLS` празно по подразбиране → blind SSRF/port-scan на backend (qdrant, ollama, portainer) | server.py:2036-2064 | АКТИВЕН (ALLOWED_ODOO_URLS празно на сървъра) |
| **G** | **identify(name=...) impersonation** на non-unified път (claude.ai OAuth / shared token): `bind_principal` по гол `name` без owner proof → пълен takeover на чужд профил (connections auto-activate, Telegram, Google, memory) | server.py:5159-5188 | АКТИВЕН за non-unified клиенти |
| **H** | **TLS изключен**: `_xmlrpc_validate` + всички OdooWebSession ползват `CERT_NONE`/`verify=False` → MITM на api_key/портал cookies | server.py:2021-2023, 1243/1274/1320/..., 594-606 | АКТИВЕН |
| **I** | **git_remote / ssh_execute command injection + lateral**: `repo_path`/`extra_args`/`custom` unquoted в shell; ssh_execute с произволен host + server ключове + agent forward | server.py:5667-5685, 5620-5644 | АКТИВЕН (зад принципал gate) |

### MEDIUM / LOW
- `_sanitize_name` не неутрализира `..`/`.` → `identify(name="..")` сочи `/data` root (single-level escape). server.py:1507-1516.
- ConnectionManager._save() пази password/api_key **plaintext** (docstring лъже). server.py:920-939.
- pdf_sanitizer fails-open (връща оригинала при parse грешка). pdf_sanitizer.py:112-179.
- /health изтича alias списъка; /metrics public.
- WAL sidecar файлове не са 0600 (нов, low). session_store.py:129.
- admin XFF spoofing/lockout bypass АКО порт 8084 е директно достъпен — **митигиран**: 8084 НЕ е изложен на хоста (само cloudflared tunnel). ✓

### Потвърдено НЕ-уязвимо (strict session моделът държи)
- Mcp-Session-Id spoofing блокиран от SDK (404 на непознат id; uuid4 122-bit).
- Principal mismatch → orphan + отказ (BEGIN IMMEDIATE, без TOCTOU).
- session_store SQL изцяло параметризиран; session_list/revoke авторизацията коректна.
- ContextVar (asyncio.to_thread) без cross-request bleed; SessionRuntime ключове уникални.
- Fail-closed accessor-ите без останал fallback.
- register-connection прави XMLRPC + owner proof (за разлика от bulk connections!).

---

## V3 — dev (НЕ deployed; DRY_RUN=1). Catastrophic-ако-се-пусне.

| # | Находка | File:line |
|---|---|---|
| V-C1 | **odoo_execute = generic method bypass на целия RBAC** (USER): denylist хваща само unlink/write/create+няколко; `copy`/`set_param`/`action_*` минават | tool_security.py:179-208 |
| V-C2 | **data-modifying CTE заобикаля SQL protected-table gate**: `WITH x AS (DELETE FROM res_users RETURNING id) SELECT...` → класифициран `select`→USER allowed (verified с sqlglot) | sql_classifier.py:97-144 |
| V-C3 | **глобален active-tenant** (`/data/active_tenant.json`) не е per-session → cross-tenant data takeover между конкурентни сесии | tenant_router.py:31,57-96,152-192 |
| V-C4 | **provision_* admin tools без role/cap gate в call_tool** (dispatch ПРЕДИ check_call) → self-issue на admin provisioning key с `MCP_SECRET_TOKEN` | server.py:4706-4715 |
| V-H1 | elevation = process-global singleton → cross-session privilege escalation; self-grant без approval | elevation.py:33-39 |
| V-H2 | MCP_ROLE default `admin` (fail-open при липсваща конфигурация) | tool_security.py:151 |
| V-H3 | internal-net rate-limit/lockout bypass (172/10/127); broad CF token (zone-wide DNS/tunnel); plaintext client tokens в provisioning_state.jsonl | provisioning_api.py:84-130; cloudflare_provisioning.py; provisioning_engine.py:436-451 |
| V-pos | template injection БЛОКИРАН (regex валидация); pepper fail-closed; compare_digest; os.replace атомарност — добре направени |

### Backport правила (v2 USER в бъдеще)
НЕ backport-вай към v2 без фикс: odoo_execute denylist (C1), SQL classifier без CTE-write fix (C2), global-singleton elevation (H1), ADMIN fail-open default (H2). Безопасни: HMAC+pepper key manager, savepoint изолация, proxy_call блокиране.

---

## Приоритети за поправка (преди `:latest` bump)

**P0 (production, веднага):**
1. **A** — премахни/redact AUTH-DEBUG header dump (пасивен непрекъснат leak на Odoo ключове).
2. **B** — owner-проверка на /api/user/connections (или премахни bulk endpoint-а).
3. **C** — `MCP_OAUTH_CLIENT_SECRET` ≠ secret_token на 8/8 (audit) + спри издаването на secret_token като access_token.
4. **D** — confine save_path под `_user_dir`, reject абсолютни/`..`.

**P1:** E (махни IP-trust за /api/ai), F (ALLOWED_ODOO_URLS whitelist), G (gate identify на non-unified), H (TLS verify по подразбиране).

**P2:** I (shlex.quote git_remote, scope ssh keys), `_sanitize_name` reject `.`/`..`, WAL sidecar 0600.

**Нужно runtime потвърждение:** `MCP_OAUTH_CLIENT_SECRET` на всичките 8 стака (C); реален source IP на CF трафика (E).
