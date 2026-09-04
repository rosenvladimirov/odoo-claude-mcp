# ADR-0004 — Ревизията затваря, не предупреждава

- **Статус:** предложен от Claude на 04.09.2026, реализиран в 3.3.8; приема се с ревюто и комита
- **Дата:** 2026-09-04
- **Контекст:** ревизия на `admin_ui.py` (2 076 реда, нула тестове, заварен от 2.24.0)
- **Решение на:** Росен (при ревю на 3.3.8)

## Проблемът

Три заварени поведения „работеха“ и затова никой не ги беше видял:

1. Конзолата се качваше и без `MCP_SECRET_TOKEN` / `MCP_ADMIN_SESSION_SECRET` — с
   низа `INSECURE-DEFAULT-SET-MCP_SECRET_TOKEN` за подпис на бисквитката. Всеки,
   който е чел кода, може да си подпише сесия.
2. Еднократният API ключ пишеше `api_key_expires` (7 дни), но никой не го четеше —
   ключът беше вечен, а UI-ят твърдеше обратното.
3. Сесия от redeem-нат ключ стигаше до `/api/connections` преди да има парола —
   dashboard-ът пренасочваше, API-то не.

Плюс по-дребните: `DATA_DIR` зашит на `/data`, докато `server.py` чете env
(два корена); lockout само по IP; URL/DB/alias без escape; „Смени парола“ = `alert()`.

## Разгледани варианти

### А. Предупреждение в лога и продължаване
Отхвърлен. Точно така стоеше `MCP_REQUIRE_AUTH=0` до 3.3.6 — предупреждението го
четохме след одита, не преди. Лог, който никой не следи, не е защита.

### Б. Fail-closed (приет)
Без secret → `get_routes()` връща `[]`: конзолата не съществува, MCP-то работи,
логът казва защо. Изтекъл ключ → `401`, без да се изгаря (админът вижда състоянието и
издава нов). `setup_pending` → само `/setup` и API-то му (през middleware-а от ADR-0003).

## Решението

| заварено | сега |
|---|---|
| placeholder secret монтира конзолата | маршрутите не се регистрират; `error` в лога |
| `api_key_expires` не се чете | проверява се преди сравнението на ключа; изтекъл = 401 |
| `setup_pending` сесия в API-тата | 403 `setup_required` навсякъде извън setup |
| `DATA_DIR = "/data"` | `os.environ.get("DATA_DIR", "/data")` — като `server.py` |
| lockout по IP | `max(по IP, по профил)` — зад NAT един не заключва всички, а с редуване на адреси не се измъква никой |
| URL/DB/alias в HTML | `html.escape` сървърно, `esc()` в JS |
| „Смени парола“ = `alert()` | реална смяна (текуща парола + код при фактор), затваря другите сесии; списък на сесиите с „Затвори останалите“ |
| `datetime.utcnow()` | `datetime.now(timezone.utc)` |

## Последици

- Стак без `MCP_SECRET_TOKEN` губи конзолата при ъпгрейд. Това е желано: никой не е
  трябвало да я държи отворена така. Симптомът е един ред в лога:
  `admin UI will not be registered`.
- Потребител с ключ от преди повече от 7 дни ще получи „изтекъл“ — админът издава нов.
- Смяната на парола затваря другите сесии на профила — това е поведението, което
  човек очаква след компрометирана парола, и досега го нямаше.

## Намерено, НЕ променено (следващ пас, отделно решение)

- `_client_ip` вярва на `X-Forwarded-For` безусловно. Зад cloudflared това е
  единственият източник, но пряк достъп до порт 8084 го подправя и заобикаля
  IP lockout-а и allowlist-а. Иска списък с доверени проксита.
- `admin_backup` / `admin_filestore` приемат `MCP_ADMIN_TOKEN` вместо парола за
  деструктивните операции, сравнен с `==` (не `compare_digest`).
- `_validate_odoo` изключва TLS проверката (документираната TOFU дупка).
- `is_admin` замръзва в сесията до 7 дни — сваляне на права не се усеща до логаут.

## Проверка след деплой

```
без MCP_SECRET_TOKEN         → лог: "admin UI will not be registered"; GET /admin → 404
ключ с api_key_expires < now → 401 "изтекъл"; api_key_hash непокътнат
redeem + GET /api/connections→ 403 setup_required; след парола → 200
5 грешни пароли от IP A      → 429 и от IP B за същия профил
connections.json с <script>  → dashboard-ът го показва като текст
```

Тестове: `test_refuses_to_mount_with_default_session_secret`, `test_expired_one_time_api_key_is_rejected`,
`test_setup_pending_session_cannot_reach_api`, `test_login_lockout_counts_per_account_not_only_per_ip`,
`test_dashboard_escapes_connection_values`, `test_password_change_needs_code_and_revokes_other_sessions`.

## Свързано

- ADR-0001…0003 в същата папка
- `specs/mcp-oauth-hardening/adr/0001` — 3.3.6, същият принцип (fail-closed default-и)
- `project_mcp_security_audit_2026_06_10` — одитът, който намери HTTP-слоя дупки на v2
