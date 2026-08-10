# ADR-0001 — OAuth се втвърдява, не се изключва

- **Статус:** приет, реализиран в 3.3.6
- **Дата:** 2026-08-10
- **Контекст:** одит на authN от 02.08.2026 (`mcp-odoo-rpc` на 62.171.156.220)
- **Решение на:** Росен

## Проблемът

Одитът намери верига, която работи от гол интернет, без никакви креденшъли:

1. `POST /oauth/register` е в `public_paths` → връща `client_id` + `client_secret`
2. `POST /oauth/token` с `grant_type=client_credentials` → минтва валиден Bearer
3. `POST /mcp` с този Bearer → `_check_auth` го приема през `_oauth_token_valid`

Тежестта е **по-ниска от „RCE на 197 tools"**, защото над всеки tool стои strict
session gate: без `sc.principal` admin tools връщат `denied no_identity`, а
`odoo_*` връщат `MCP_NO_CONNECTION`. Нападателят държи HTTP слоя, не данните.
Но целият клас се затваря тук.

Успоредно: `/oauth/authorize` приемаше **всеки** `redirect_uri` при празен
allowlist, а `code_challenge` не се проверяваше никъде.

## Разгледани варианти

### А. Kill switch — махни OAuth от `public_paths`
Отхвърлен. **Чупи claude.ai native конектора**, който Росен ползва. Той се
авторизира точно през OAuth: `register` → `authorize` → `token` → `/mcp`.
Изключването му отрязва един от трите „уеб Клод"-а.

### Б. Cloudflare Access пред `*.odoo-shell.space`
Отхвърлен като основен лек. Решение на Росен (02.08): **само вътрешни
авторизации в MCP** — нищо външно, без разчитане на CF Access, ufw или
Contabo firewall. Външният слой е добавка, не фундамент.

### В. Втвърдяване (прието)
Затваряме **употребите**, не протокола. Живият claude.ai flow (заснет 09.08)
показва `authorization_code` + PKCE `S256` и **никога** `client_credentials`.
Значи двете ключалки долу не му пречат.

## Решението

1. **`client_credentials` изключен по подразбиране.** Това само по себе си
   къса веригата `register` → `token`. Обратим с
   `MCP_OAUTH_ALLOW_CLIENT_CREDENTIALS=1` **без rebuild**.
2. **PKCE S256 задължителен** на `/oauth/authorize`; `/oauth/token` проверява
   `code_verifier` срещу вързания challenge. Прихванат код сам по себе си
   става безполезен. Обратим с `MCP_OAUTH_REQUIRE_PKCE=0`.
3. **Метаданните казват истината** — `client_credentials` се обявява само
   когато реално е включен, за да не се проваля преговорът чак на `/token`.
4. **`/metrics` зад `_check_auth`** — анонимно течаха имена на tools, бройки
   извиквания и tenant кодове.
5. **`who_am_i` маскира тайните** — връщаше `api_key` и `portainer.token` в
   открит текст на всеки викащ.

## Защо флагове, а не твърдо зашито

Кодовият фикс иска rebuild+redeploy; env флагът иска само recreate. Ако
претърпи регресия клиент, който не сме предвидили, връщането е една променлива,
не нов образ посред нощ. Затова и двете ключалки са env-превключваеми, а
default-ите са fail-closed.

## Последици

- Всеки бъдещ OAuth клиент **трябва** да праща PKCE. Това е RFC-съобразно и
  claude.ai вече го прави; но клиент, писан по стария начин, ще получи 400.
- `/metrics` иска токен → ако някога се добави Prometheus scraper, той трябва
  да носи `MCP_SECRET_TOKEN`.
- `who_am_i` вече не е начин да си извадиш ключа. Ключовете се четат от
  `/data/users/<principal>/connections.json` вътре в контейнера.

## Проверка след деплой

```
grant_types_supported          → само ["authorization_code"]
client_credentials на /token   → 400 unsupported_grant_type
authorize без code_challenge   → 400 invalid_request
authorize с method=plain       → 400 invalid_request
/metrics анонимно              → 401
who_am_i                       → api_key/token == "<set>"
claude.ai конектор             → работи (пълен flow 200)
```

## Свързано

- `gotcha_mcp_v3_unauth_oauth_dcr_bypass_and_authn_audit_2026_08_02` (одитът)
- ADR-0002 в `specs/mcp-supervisor-remote/` (същият принцип: втвърди транспорта,
  не го махай)
