# till-enroller — услугата за записване на касите

Решения: ADR erpnet-kasi-fleet/0012 (услугата държи CF токена), 0013 (LAN името), 0010 (касата се
записва с единния ключ). Договор: `claude.ai/specs/erpnet-kasi-fleet/contract/CONTRACT_v1.md`, секция ENR.
Тестовите вектори са `tests/vectors_v1.json` — същите байтове като в договора.

Услугата не взема решения. На всеки 30 s тегли от лицензионния сървър (LS) какво да направи:

| действие | какво прави | при провал |
|---|---|---|
| `create` | тунел `erpnet-<клиент>-<каса>` → маршрут `<каса>.<клиент>.erpnet.mcpworks.net` → токен на тунела → cert-issuer `POST /v1/admin/tills` (тайната за certpull) → LS `report` (подписано разрешение, `reg`) → EFP1 v2 с `crt`, запечатан с `box_pub` → LS `store` → `done` | rollback в обратен ред, после метене по списъка в CF и сверка; `done{ok:false}` с бележка какво е останало |
| `lan` | DNS-only `A <каса>.<клиент>.lan.mcpworks.net → lan_ip` (RFC1918) → SAN в cert-issuer → `done` | новият запис се трие, старият адрес се връща |
| `retire` | маха маршрута, тунела, DNS записа и касата в cert-issuer, сверява с CF → `done` | `done{ok:false}` със списък на оставеното |

Всяко действие се отчита към LS с `done` (ok + бележка без тайни) — одитът е в `l10n.bg.license.till`.

**Заварен тунел или маршрут със същото име не се ползва и не се трие** — може да е жива каса,
направена от лаптопа (`kasi.py`, осиновяване по ADR-0014). Записът получава `done{ok:false}`
„already exists“ и човек решава.

## Env

| променлива | какво |
|---|---|
| `CF_API_TOKEN` | токенът на услугата (виж правата долу) |
| `CF_ACCOUNT_ID`, `CF_ZONE_ID` | акаунтът (`rvladimirov`) и зоната `mcpworks.net` |
| `LS_URL`, `LS_API_KEY` | лицензионният сървър; API ключ на потребител от `group_till_provisioner` (`Authorization: Bearer`) |
| `CI_URL` | cert-issuer **през Access** (публичното име зад Access, не `http://cert-issuer:8095` — вътрешният адрес няма JWT и admin API-то отказва) |
| `CI_ACCESS_CLIENT_ID`, `CI_ACCESS_CLIENT_SECRET` | service token-ът в Access за admin API-то на cert-issuer |
| `CI_PULL_URL` | адресът, от който касата тегли сертификата (`cu` в EFP1); по подразбиране `CI_URL` |
| `ENR_SSH_PUBKEYS` | SSH ключове за `ssh` в EFP1, по един на ред (по избор) |
| `ENR_INTERVAL_S` | 30 |
| `ENR_DRY_RUN=1` | без CF повиквания, с фалшиви id-та. **LS и cert-issuer са истински** — само срещу тестов LS |

Тайните са само от env. В лога не влизат: кодът не ги логва, а филтърът `Redactor` сменя всяка
позната тайна (токените от env и токените на тунелите в движение) с `***` и не пуска трасета.

## Правата на CF токена

Отделен токен, не този от лаптопа — отнема се самостоятелно.

- **Account → Cloudflare Tunnel → Edit** (тунели и токените им);
- **Account → Zero Trust → Edit** (маршрутите по име, `/zerotrust/routes/hostname`) — кое точно право
  иска този път с отделен токен, не е мерено (фаза 0 мина с токена `rvladimirov`); проверява се при създаването;
- **Zone → DNS → Edit**, само за зоната `mcpworks.net`.

CF не дава права по поддърво, затова DNS токенът може да пише навсякъде в `mcpworks.net`.
**Ограничението до `<каса>.<клиент>.lan.mcpworks.net` е в кода** (`check_lan_name`, константа
`LAN_SUFFIX`, не env) и се проверява във всеки DNS метод, преди заявката. Тестът
`test_dns_outside_the_lan_zone_is_refused_before_any_call`.

## Пускане (ръчно, от Росен)

1. CF токен с правата горе → `TILL_ENROLLER_CF_API_TOKEN`, `…_CF_ACCOUNT_ID`, `…_CF_ZONE_ID`.
2. Access: приложение за admin API-то на cert-issuer (`/v1/admin/*` на името му) с политика
   **Service Auth** само за нов service token; в cert-issuer — `CERT_ISSUER_ACCESS_TEAM_DOMAIN`,
   `CERT_ISSUER_ACCESS_ADMIN_AUD`, `CERT_ISSUER_ACCESS_ADMIN_CLIENT_IDS`; тук — `TILL_ENROLLER_CI_URL`,
   `…_CI_ACCESS_CLIENT_ID`, `…_CI_ACCESS_CLIENT_SECRET`.
3. LS: потребител в `group_till_provisioner`, API ключ → `TILL_ENROLLER_LS_URL`, `…_LS_API_KEY`.
4. DNS: нищо предварително — записите под `lan.mcpworks.net` услугата ги прави сама.
5. Първо `TILL_ENROLLER_DRY_RUN=1` срещу тестов LS; после `0`.

```
docker compose up -d till-enroller
docker exec till-enroller python /app/enroller.py once     # един проход
```

## Тайната за certpull (договор, поправка 1.1)

cert-issuer връща тайната на касата при `POST /v1/admin/tills`. Услугата я слага като `crt` в EFP1
преди запечатването и не я пази другаде: не е в паметта след прохода, не е в бележките към LS, не е
в лога (`Redactor` я знае, ако някой я логне по грешка). Без тайна в отговора — rollback.
`lan_ip` идва в записа от `pull` (поправка 1.1); без него `lan` → `done{ok:false}`.

## Отворено

- Маршрутите се четат с `per_page=1000` — над това трябва страниране.

## Тестове

```
python3 -m venv .venv-enr && .venv-enr/bin/pip install pytest "cryptography>=42,<47"
.venv-enr/bin/python -m pytest -q till-enroller/tests
```

Моковете на LS, CF и cert-issuer са истински HTTP сървъри на `127.0.0.1` (`tests/conftest.py`).
