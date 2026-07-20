# ADR-0002: Sidecar exec transport за Cloudflare-fronted Portainer хостове

- **Статус:** Приет
- **Дата:** 2026-07-20
- **Контекст на решението:** `supervisor_deploy` (MCP v3.3.4)
- **Свързан:** [ADR-0001 — транспорт през Portainer exec](0001-transport-via-portainer-exec.md)

## Контекст

ADR-0001 избра Portainer Docker API като транспорт, реализиран като **one-shot
контейнер**: pull → create → start → wait → logs → remove.

При живия тест срещу `mozu` / `ic-intracom.bg` (18.07.2026) този модел се оказа
неработещ. Средата:

- Portainer (`portainer.mozu.bg`) е зад **Cloudflare tunnel**
- Docker **29.6.2** (API 1.55), въртящ се в **Proxmox LXC контейнер CT20010**

Bodyless `POST /containers/{id}/start` се пре-chunk-ва по пътя през тунела →
Docker вижда `ContentLength=-1`, интерпретира го като непразно тяло и връща
`400 non-empty request body removed in v1.24`.

Изпробвани и отхвърлени варианти на самата заявка (и четирите → 400):
гол POST, явен `Content-Length: 0`, `--data-binary ''`, стрип на
`Transfer-Encoding`. Форсирането на `content=b""` в httpx също не помага —
пре-chunk-ването е по пътя, не при клиента.

Отпада и заобикалянето през по-стар API префикс (`/v1.23/containers/.../start`,
където проверката за тяло липсва): Docker 29 върви с `DOCKER_MIN_API_VERSION=1.24`,
а проверката започва точно от 1.24.

Ключово наблюдение: **само bodyless POST страда**. `create`, `exec`, `archive`,
`images/create` — всичко с тяло — минава чисто. Това беше доказано на живо:
реалният git drift скан на 229 репа мина през `exec` в вече работещия
`bash-git-bash-1`.

Страничен ефект от същия дефект: предишните „exit 0 + празен лог" резултати са
били защото контейнерът **никога не е стартирал**.

## Решение

Добавяме втори транспорт — **sidecar exec** — и го правим препоръчаният:

1. `GET /containers/{name}/json` → проверка `State.Running`
2. `POST /containers/{name}/exec` (JSON тяло) → exec id
3. `POST /exec/{id}/start` с `{"Detach": false, "Tty": false}` → блокира до край
4. Демултиплексиране на 8-байтовия stdout/stderr framing
5. `GET /exec/{id}/json` → `ExitCode`

Изборът е per-alias чрез `supervisor.transport`: `auto` (по подразбиране) |
`sidecar` | `oneshot`. При `auto` наличието на `supervisor.sidecar` избира
sidecar пътя — така заварените alias-и остават на доказания one-shot.

**Sidecar-ът не се стартира от MCP.** Вдигането на постоянен контейнер изисква
точно блокирания `start`, така че bootstrap-ът е еднократна ръчна операция от
оператора (Portainer UI или хост shell). `supervisor_sidecar_status(target)` е
read-only и връща готовия `docker run` ред, когато контейнерът липсва или е спрян.

## Последствия

**Плюсове**

- Работи зад Cloudflare tunnel и на Docker >= 29
- По-бързо: без pull/create/remove на всяко извикване
- Логовете идват директно от exec потока — без зависимост от log драйвера на хоста

**Минуси**

- Изисква еднократен ръчен старт на sidecar-а за всеки такъв alias
- Sidecar-ът виси постоянно (нищожен ресурс при `sleep infinity`)
- Bind mount-овете са фиксирани при старта — смяна на binds иска пресъздаване
- Дълъг живот на контейнера → образът застоява; ъпдейт = ръчно пресъздаване

**Отхвърлени алтернативи**

- *Вариации на самата `start` заявка* — и четирите дават 400 (виж по-горе)
- *По-стар API префикс* — блокиран от `DOCKER_MIN_API_VERSION=1.24`
- *Ползване на заварения `bash-git-bash-1`* — монтира `/opt/odoo/odoo-19.0` на
  `/workspace` (пътищата в `addons.conf` не съвпадат) и няма гаранция за python3;
  качването на `supervisor.py` през `PUT /containers/{id}/archive` работи, но не
  оправдава несъответствието на пътищата
- *SSH до хоста* — операторът е „затворен в Portainer" (предпоставката на ADR-0001)

## Бележка за миграция

One-shot транспортът се запазва в 3.3.4 и **отпада, щом sidecar пътят се докаже
в производствена среда** (решение на Росен, 20.07.2026). Дотогава
`odoo-dev-server` остава на one-shot без промяна.
