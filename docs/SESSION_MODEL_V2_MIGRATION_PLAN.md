# План: Пълна миграция към строг сесиен модел — MCP v2.30.0

> Статус: ОДОБРЕН ЗА АВТОНОМНО ИЗПЪЛНЕНИЕ (подготвен 2026-06-10).
> Обхват: **само v2** (`~/Проекти/odoo/odoo-mcp/odoo-rpc-mcp`). v3 чака стабилизация на v2.
> Принцип: БЕЗ кръпки, БЕЗ legacy режим, БЕЗ файлов/глобален fallback.
> Ако за tool call няма сесийни данни → **отказ с ясно съобщение** (fail-closed).

---

## 0. Фиксирани решения (без отворени въпроси — автономен режим)

| Параметър | Стойност | Обосновка |
|---|---|---|
| Версия | **2.30.0** (от 2.26.1) | breaking milestone в 2.x; 3.x е запазен за integrator track |
| Работен branch | **`feat/session-model-strict`** от `feat/per-session-connection` HEAD (`61cb6b7`) | не пипа 2.0 prod branch до стабилизация |
| Session store | SQLite **`/data/mcp_sessions.db`** (WAL), env `MCP_SESSIONS_DB` | ОТДЕЛНА от `/data/sessions.db` (terminal window registry — НЕ се пипа) |
| Session key | `Mcp-Session-Id` header (streamable HTTP) / `session_id` query param (SSE) | вградена cookie семантика на протокола; клиентите го echo-ват задължително; verified в mcp SDK 1.27.0 (`RequestContext.request` носи Starlette Request с headers) |
| Auth | Bearer (unified-auth или secret_token) — валидиран всяка заявка, както сега | auth = кой си; session id = коя сесия си |
| TTL | sliding, env `MCP_SESSION_TTL`, default **86400s** (24h) | touch на всеки tool call |
| Orphan retention | soft-delete (status `orphaned`), purge след **7 дни** (`MCP_SESSION_RETENTION_DAYS=7`) | audit trail |
| Reaper | inline (на ≤15 мин при tool call) + при startup; БЕЗ отделен thread | детерминистично |
| Feature flag | **НЯМА** (`MCP_SESSION_MODEL` не се въвежда) | dual-mode = запазване на fallback клоновете = кръпка. Rollback = image pin `:2.26.1` |
| `SINGLE_CONNECTION` | остава като explicit deployment opt-in (НЕ е fallback) | single-tenant кутии |
| Python executor | `run_in_executor` → **`asyncio.to_thread`** (копира contextvars) | решава ContextVar загубата структурно |
| mcp SDK pin | `mcp>=1.16.0` в requirements.txt | нужен `RequestContext.request` |

---

## 1. Архитектура (резюме; пълният дизайн е верифициран срещу кода)

Два слоя:

1. **`session_store.py`** (НОВ модул) — SQLite source of truth за идентичност и декларативно състояние: principal, активна конекция (дескриптор без секрети), Telegram phone, web session cookie.
2. **`SessionRuntime`** (в session_store.py) — in-process кеш на живи обекти (OdooConnection, OdooWebSession, Telethon клиенти, SSH masters), keyed по `session_key` (Telegram: по `principal:phone`). Rehydrate от store при miss; evict при revoke/expire/orphan.

### 1.1 Схема (schema_version=1, `_MIGRATIONS` механизъм за бъдещи)

```sql
PRAGMA journal_mode=WAL;
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions (
    session_key   TEXT PRIMARY KEY,           -- 'mcp:<hex>' | 'sse:<hex>'
    transport     TEXT NOT NULL CHECK (transport IN ('streamable_http','sse')),
    principal     TEXT,                       -- mcp_user safe name; NULL до bind
    principal_src TEXT CHECK (principal_src IN ('unified_auth','identify')),
    auth_fp       TEXT,                       -- sha256(url|db|login|api_key) при unified_auth
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','orphaned','revoked')),
    created_at    TEXT NOT NULL, last_seen TEXT NOT NULL, expires_at TEXT NOT NULL,
    orphaned_at   TEXT, orphan_reason TEXT,   -- 'ttl_expired'|'server_restart'|'admin_revoke'|'principal_mismatch'
    client_info   TEXT                        -- JSON {user_agent, remote}
);
CREATE INDEX idx_sessions_principal ON sessions(principal);
CREATE INDEX idx_sessions_status_exp ON sessions(status, expires_at);
CREATE TABLE session_state (
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
    namespace   TEXT NOT NULL,   -- 'connection' | 'telegram' | 'web'
    key         TEXT NOT NULL,   -- 'active' | 'phone' | <alias>
    value       TEXT NOT NULL,   -- JSON
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (session_key, namespace, key)
);
```

API: `create / resolve / touch / bind_principal / revoke / get_state / set_state / delete_state / list_sessions / mark_orphaned / cleanup`. Exceptions: `SessionError(error_code, how_to_fix)` → `SessionNotFound`, `SessionPrincipalMismatch`. Thread-safety: connection-per-call + `BEGIN IMMEDIATE` в `bind_principal`; busy timeout 10s. DB файл mode 0600 (web cookie е секрет at rest).

### 1.2 Session resolution (замества `_get_current_user`/`_get_mcp_session_key`)

```python
@dataclass(frozen=True)
class SessionContext:
    session_key: str; transport: str
    principal: str | None; principal_src: str | None
    caller: dict | None   # пълният unified-auth dict

_session_ctx: ContextVar[SessionContext | None]
async def resolve_session_context() -> SessionContext   # само от call_tool
def _sctx() -> SessionContext                            # sync accessor, raise при None
def _require_principal() -> str                          # навсякъде вместо _get_current_user
```

Алгоритъм (в async task-а, където `request_ctx` Е наличен):
1. `req = mcp_server.request_context.request` → `mcp-session-id` header → `mcp:<sid>`; иначе `session_id` query param → `sse:<sid>`; иначе → `MCP_NO_SESSION`.
2. Headers съдържат `x-odoo-url` → `get_caller_odoo_user()` (кеширан) → невалиден = `MCP_AUTH_FAILED`; без тях (claude.ai OAuth) → `caller=None` (HTTP gate-ът вече е минал).
3. Store: няма ред → `create(...)` (фалшив session id не стига дотук — SDK-то връща 404 преди това); има ред → `touch()`; `caller` + празен principal → `bind_principal(unified_auth)`; `caller` + ДРУГ principal → `MCP_SESSION_PRINCIPAL_MISMATCH` + `mark_orphaned(principal_mismatch)`.
4. Правило за имплементацията: sync кодът чете САМО `_session_ctx` през `_sctx()` — никога `request_context` директно.

### 1.3 Единен gate (server.py `call_tool`, ~4354-4386)

```python
sc = await resolve_session_context()
token = _session_ctx.set(sc)
try:
    result = await asyncio.to_thread(_execute_tool, name, arguments)   # беше run_in_executor
finally:
    _session_ctx.reset(token)
```

Gate-ът установява сесия за **100% от tools** (вкл. proxy). Изискванията principal/connection/phone се enforce-ват в **accessor-ите** (`_require_principal`, `_conn`, `_tg`, `_get_web_session`, `_google`) — без матрица tool→правило. `SessionError` се сериализира като нормален tool result:

```json
{"error": "...", "error_code": "MCP_NO_SESSION|MCP_NO_IDENTITY|MCP_NO_CONNECTION|MCP_NO_TELEGRAM_PHONE|MCP_AUTH_FAILED|MCP_SESSION_PRINCIPAL_MISMATCH|MCP_SESSION_ORPHANED",
 "how_to_fix": "точна инструкция с tool име и аргументи"}
```

Канонични съобщения (констатирани веднъж, EN — UI правило):
- `MCP_NO_IDENTITY`: "No identity is bound to this MCP session. Either reconnect with unified-auth headers (Authorization: Bearer <odoo_api_key> + X-Odoo-Url/Db/Login) or call identify(name='<your profile>') once in this session."
- `MCP_NO_CONNECTION`: "No active Odoo connection in this session. Call identify(...) to auto-activate your saved connection, or odoo_connect(...), or user_connection_activate(alias=)."
- `MCP_NO_TELEGRAM_PHONE`: "No Telegram account is bound to this session. Call telegram_auth(phone='+359...') first — if this phone was authorized before, no SMS code is needed."
- `MCP_NO_SESSION`: "No MCP session established. Re-initialize the MCP connection (the client must echo the Mcp-Session-Id header). If the server was restarted, reconnect the client."
- `MCP_SESSION_ORPHANED`: "This MCP session has expired or was orphaned by a server restart. Reconnect the MCP client to start a fresh session."

Bootstrap tools (изискват само сесия, не principal): `identify`, `who_am_i`, `odoo_connect`, `odoo_version`, `odoo_connections`. `telegram_auth/auth_status/configure` и `google_auth/auth_status` изискват **principal** (пишат per-principal файлове). Всичко останало — през accessor-ите.

### 1.4 Миграция на състоянието по групи

| Група | Source of truth | Runtime | Какво се ТРИЕ |
|---|---|---|---|
| **Connection** (~85 tools) | `(sk,'connection','active')` = `{source:'user_profile',user,alias}` или `{source:'inline',url,db,username,protocol}` — БЕЗ секрети; user_profile се rehydrate-ва от `users/<p>/connections.json`, inline при miss → `MCP_NO_CONNECTION` | `SessionRuntime._conns[sk]` | `_session_conns`+lock+3 helpers (1367-1386); fallback `manager["default"]` (1054); тихият single-connection клон в `ConnectionManager.get` (~954) |
| **Telegram** (10 tools) | `(sk,'telegram','phone')`; registry key `f"{principal}:{tag}"`; `session_path=users/<p>/telegram_<tag>`; `config_file` ВИНАГИ подаден | Telethon клиенти per `principal:phone` (refcount, виж §2) | `_session_tg_phone` (1359); ДВАТА `__global__` клона в `_tg` (1420-1431); `__global__` default в `_tg_subs_file` |
| **Google** (11 tools) | token `users/<p>/google_token.json`; нов `GoogleRegistry.for_user(principal)` (огледало на TelegramRegistry); `GoogleServiceManager.__init__(token_file, credentials_file)` параметризиран | per principal | глобалът `google_mgr` (971, init ~8995) |
| **Web** (7 tools) | `(sk,'web','<alias>')` = `{url,db,login,session_cookie,uid}`; rehydrate чрез cookie injection; expired → трий ред + error | `SessionRuntime._web[(sk,alias)]` | `_web_sessions` (1172), `_web_session_key` (1337) |
| **SSH** (2 tools) | няма DB запис (живи OS ресурси) | ключ `(principal,user,host,port)`; ControlPath `/tmp/.ssh-mux/<principal>/...` 0700 | анонимният достъп (изисква `_require_principal`) |
| **Identity** | sessions.principal | — | `_session_users` (1354) вкл. `"current"` (1691-1692, писан на 4863-4864, 9802); `_get_current_user` (1666); `_get_mcp_session_key` (1657); `_odoo_caller_ctx` (1709 + set на 9087) |

`identify` (4835): unified-auth клон чете `_sctx().caller`; claude.ai клон → `bind_principal(sk, name, 'identify')`, mismatch → error. `who_am_i` (4903): чете `_sctx()` + store; НЕ error-ва при липсваща идентичност (диагностика + инструкция).

Legacy данни: in-memory dict-овете са ephemeral — **нищо за мигриране**. Telethon файлове `users/<p>/telegram_<tag>` остават валидни (re-auth без SMS). `/data/telegram_session*`, `/data/google_token.json`, `/data/telegram_subscriptions.json` (глобалните) се ИЗОСТАВЯТ — release note с ръчна `mv` инструкция, БЕЗ авто-миграция (не знаем чии са).

---

## 2. Осиротели сесии — пълен lifecycle (изискване на Росен)

Дефиниция: сесия е **осиротяла**, когато клиентът никога няма да се върне с този session id: (а) изтекъл sliding TTL; (б) рестарт на сървъра (SDK-то пази транспортите in-memory → всеки стар `Mcp-Session-Id` получава 404 и клиентът re-initialize-ва с нов); (в) principal mismatch; (г) admin revoke.

### 2.1 Откриване и маркиране

- **TTL**: `resolve_session_context` отказва редове с `expires_at < now` → `mark_orphaned('ttl_expired')` + `MCP_SESSION_ORPHANED`.
- **Рестарт**: при startup (`create_app`) ВСИЧКИ редове със status `active` → `mark_orphaned('server_restart')` (никой от тях не може да се възобнови — транспортът е mortal). Това е и одитен запис кога е имало рестарт.
- **Mismatch/revoke**: на място, със съответния reason.

### 2.2 Teardown на ресурсите (детерминистичен, при mark_orphaned + при reaper pass)

| Ресурс | Действие при осиротяване |
|---|---|
| `SessionRuntime._conns[sk]` | drop (OdooConnection няма persistent socket — GC стига) |
| `SessionRuntime._web[(sk,*)]` | drop + по желание logout call (best-effort, не блокира) |
| Telethon клиент `principal:phone` | **refcount**: брой active сесии с този `(principal,phone)` в store. 0 сесии И празен subscriptions allow-list → `disconnect()` (освобождава SQLite lock-а — лекува „database is locked" завинаги). 0 сесии, НО непразен subscriptions list → клиентът ОСТАВА жив (Centrifugo push е per principal:phone, независим от MCP сесия — това е feature, не leak) |
| SSH masters | `ssh -O exit` за ControlPath-ове на principal без нито една активна сесия |
| `session_state` редове | остават до purge (CASCADE при изтриване на sessions реда) |

### 2.3 Reaper

- Inline: в `resolve_session_context`, ако `monotonic() - _last_reap > 900` → (1) mark TTL-изтеклите orphaned + teardown; (2) DELETE редове с `orphaned_at < now - retention` (7 дни); (3) `SessionRuntime.evict(deleted_keys)`.
- Startup: пълен pass + restart-marking (2.1).
- БЕЗ background thread — мъртвите редове не вредят, чистенето при първото извикване е достатъчно.

### 2.4 Видимост и администрация

- Нов tool **`session_list`** (admin: изисква `MCP_ADMIN_TOKEN` Bearer или unified-auth principal с admin маркер): всички сесии със status/principal/възраст/state namespaces. Не-admin: само собствените (по principal).
- Нов tool **`session_revoke(session_key)`** (admin) → `mark_orphaned('admin_revoke')` + teardown. Лекарството за „рестартирай целия стак заради забита Telegram сесия" става хирургическо.
- `who_am_i` показва: session_key (съкратен), principal, created/last_seen/expires, активна конекция, telegram phone (маскиран), web aliases.
- `metrics.py`: gauges `mcp_sessions_active`, `mcp_sessions_orphaned`, counter `mcp_sessions_reaped_total{reason}`, `mcp_tool_session_denied_total{error_code}`.

### 2.5 Осиротели ФАЙЛОВЕ (отделно от сесиите)

`users/<p>/telegram_*`, `google_token.json` на principals без сесия от N дни: **report-only** — `session_list(include_files=True)` показва кандидати; НИКАКВО авто-триене (fail-safe; файловете са дълготрайна идентичност, не сесийно състояние).

---

## 3. Фази на изпълнение (автономен агент)

> Всяка фаза завършва със своите verification стъпки. Фаза не започва, ако предишната не е зелена. При неочаквано блокиращо противоречие с този план → СТОП + доклад (не импровизирай).

### Фаза 0 — Preflight
- `git -C ~/Проекти/odoo/odoo-mcp checkout -b feat/session-model-strict 61cb6b7` (или текущия HEAD на feat/per-session-connection).
- Baseline: `pytest odoo-rpc-mcp/tests/ -v -k "not unified_auth"` → запиши резултата (някои тестове може да искат env — отбележи кои skip-ват).
- Snapshot на grep gate списъка (§4) → файл `docs/session_migration_baseline.txt`.

### Фаза 1 — `session_store.py`
- Имплементирай схемата, SessionStore, SessionRuntime, exceptions, `_MIGRATIONS`, orphan API (mark_orphaned/cleanup/refcount helpers) по §1.1 и §2.
- Unit tests `tests/test_session_store.py` (tmp DB): create/resolve/touch/TTL expiry/bind idempotent/bind mismatch/orphan mark/retention purge/CASCADE/конкурентни writes (ThreadPoolExecutor 16 workers × 200 ops без грешка).
- ✔ `pytest tests/test_session_store.py -v` зелен.

### Фаза 2 — Gate + resolution
- `SessionContext`, `_session_ctx`, `resolve_session_context`, `_sctx`, `_require_principal`, error константите; rewrite на `call_tool` (gate + `asyncio.to_thread`); `SessionError` → JSON result + `metrics.observe_tool_call(name,"session_denied")`.
- Middleware: махни `_odoo_caller_ctx.set` страничния ефект (9087) — остава само HTTP 401 gate.
- ✔ Сървърът стартира локално (`python server.py` с tmp DATA_DIR); `curl /health` 200; MCP initialize + tools/list през `npx @modelcontextprotocol/inspector` или curl JSON-RPC; tool call без identity → `MCP_NO_IDENTITY` JSON.

### Фаза 3 — Миграция на състоянието (ред: connection → telegram → web → google → ssh)
- По таблицата §1.4 + teardown куките §2.2. Google: нов `GoogleRegistry` в google_service.py.
- Всеки под-етап: пусни unit tests + стартирай сървъра + smoke на 2-3 tools от групата (срещу mock/без реален Odoo, доколкото е възможно — резолюционните грешки са тестваеми без backend).
- ✔ Никое от изтритите имена не се реферира (междинен grep).

### Фаза 4 — identify / who_am_i / HTTP endpoints + изтриване на legacy символите
- Rewrite по §1.4; изтрий ВСИЧКИ символи от grep gate списъка (§4); `/api/identify` и `/api/connect` ендпойнтите се адаптират (те нямат Mcp-Session-Id → документирай, че работят само за claude-terminal регистрация, не за MCP session state; ако пишеха в `_session_users` (9802) — клонът пада).
- ✔ Grep gate (§4) чист.

### Фаза 5 — Осиротели сесии
- Reaper (inline + startup), restart-marking, session_list/session_revoke tools (+ Tool schemas), who_am_i разширение, metrics.
- Unit: restart simulation (нов SessionStore върху същия файл → всички active станаха orphaned); refcount teardown на фалшив Telethon-двойник (inject mock в SessionRuntime).
- ✔ pytest зелен.

### Фаза 6 — Тестове и документация
- `tests/test_session_isolation.py` (integration, стартира сървъра в subprocess на свободен порт):
  1. Две паралелни MCP сесии (две initialize → два Mcp-Session-Id) с ЕДНАКВИ headers → идентични principals, НО независими `connection/active` и `telegram phone` state редове.
  2. Tool call с невалиден/липсващ session id → SDK 404 / `MCP_NO_SESSION`.
  3. Без identity → `MCP_NO_IDENTITY`; след identify → минава.
  4. Рестарт на subprocess-а → старият session id → 404; нов initialize → чиста сесия; в DB старият ред е `orphaned/server_restart`.
  5. TTL=2s сесия → изчакай → `MCP_SESSION_ORPHANED` + ред маркиран.
- Обнови `tests/test_unified_auth.sh`: добави T11 (двама с общ ключ → изолация), T12 (no-session refusal).
- `CHANGELOG.md` запис 2.30.0 (breaking: изисквания, рестарт поведение, legacy файлове, SINGLE_CONNECTION бележка) + release notes секция в README ако има.
- ✔ Всички тестове зелени.

### Фаза 7 — Packaging
- Dockerfile: `COPY --chmod=644 session_store.py .` (КАПАН: file-by-file COPY!); requirements: `mcp>=1.16.0`; `__version__="2.30.0"` (server.py:16) + `VERSION` файл.
- `docker build -t vladimirovrosen/odoo-rpc-mcp:2.30.0 odoo-rpc-mcp/` → контейнерен smoke: up с tmp volume, /health, initialize, no-identity refusal, проверка че `/data/mcp_sessions.db` се създава с 0600.
- Commit-и: поименно стейджване (НИКОГА `git add -A`), логически разделени (store / gate / групи / orphans / tests / packaging).
- ✔ Image build + smoke зелени. **БЕЗ push към Docker Hub и БЕЗ git push** — финален доклад до Росен.

### Фаза 8 — Стабилизация и rollout (ИЗВЪН автономния обхват — изисква Росен)
- Canary: 1 poligroup стак (предложение: 131071078 BL Consulting — установено канарче) с `docker compose --env-file stack.env up -d` (🚨 stack.env капан; 202588745 иска `-p mcp-202588745`).
- Наблюдение 2-3 дни: metrics, „database is locked" да изчезне, Любо/Влади сценарият (общ ключ, два телефона).
- После останалите 7 стака; rollback = pin `:2.26.1`.
- **v3 lockstep ЕДВА СЛЕД стабилизация** (push само към `feat/per-session-connection-3.0` — branch капан!). v3 бонус: session store-ът е готовият дом за per-session elevation/tenant (днес global singletons).

---

## 4. Grep gates (финална проверка след Фаза 4 — задължителна)

```bash
cd ~/Проекти/odoo/odoo-mcp/odoo-rpc-mcp
grep -n "_session_users\|_session_tg_phone\|_session_conns\|_get_mcp_session_key\|_get_current_user\|_odoo_caller_ctx\|_web_sessions\|_web_session_key\|google_mgr\b" server.py
# Очаквано: 0 реда (или само коментари в CHANGELOG-стил — недопустимо в код)
grep -n "__global__" server.py telegram_service.py
# Очаквано: 0 реда
grep -n 'get("default")' server.py | grep -v "args.get"
# Очаквано: само SINGLE_CONNECTION клона (изричен, коментиран)
```

Плюс: `grep -n "run_in_executor" server.py` → само неосновни места (proxy discovery 4375 е ок — не пипа session state) или 0.

## 5. Граници на автономния агент

- РАБОТИ: код, тестове, локален docker build/smoke, commit-и в `feat/session-model-strict`.
- НЕ ПРАВИ без изрично одобрение: git push, Docker Hub push, деплой на клиентски стакове, триене на данни в `/data` на сървъри, промени по v3, промени по claude-terminal/start-session.sh (не са нужни по дизайн — нулев клиентски ефект за unified-auth клиенти).
- При блокаж (SDK поведение различно от описаното, счупен baseline тест, противоречие в плана): СТОП + доклад с конкретика.

## 6. Източници / контекст

- Дизайнът е верифициран срещу: mcp SDK 1.27.0 (`streamable_http.py:260-266/518/541`, `sse.py:210/244`, `streamable_http_manager.py:244/307-320`, `lowlevel/server.py:753-766`), server.py (10254 реда, line refs към HEAD `61cb6b7`).
- Известни клиенти: claude-terminal (.mcp.json unified-auth headers — нулева промяна), claude.ai connector (OAuth secret_token → ще иска identify веднъж на сесия), SSE legacy (session_id query param), Odoo iframe `/api/session/*` (друг домейн, не се пипа).
- Памет: project_mcp_telegram_subscriptions_2026_06_05 (stack.env капан, 8 стака), anchor_telegram_identify_first_for_client_claudes (Любо/Влади сценарий = acceptance тест).
