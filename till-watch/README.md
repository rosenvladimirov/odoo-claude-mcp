# till-watch — наблюдателят на касите

ADR erpnet-kasi-fleet/0017. Пита `/healthz` на всяка записана каса през тунела ѝ
и отчита в лицензионния сървър (odoo-2026, **Лиценз › Клиенти › Каси**).

## Какво трябва

- **Cloudflare Zero Trust:** service token `till-watch` и включващо правило за него
  в „Warp Login App“ (записване на устройства). Организацията е team domain-ът.
- **Лицензионният сървър:** потребител в група **Till Watcher**, без парола, с API ключ (rpc).
- **Машината:** `/dev/net/tun`. WARP живее само в мрежата на контейнера.

## `.env` (0600)

```
LS_URL=https://www.odoo-shell.dev
LS_API_KEY=...
WARP_ORG=<team name>
WARP_CLIENT_ID=...
WARP_CLIENT_SECRET=...
```

По избор: `WATCH_INTERVAL_S` (60), `WATCH_TIMEOUT_S` (8), `WATCH_PORT` (8001), `WATCH_WORKERS` (16).

## Пускане

```
mkdir -p /opt/till-watch/build && cp Dockerfile entrypoint.sh watch.py /opt/till-watch/build/
cp docker-compose.yml /opt/till-watch/  # build: ./build
cd /opt/till-watch && env -u SSH_AUTH_SOCK docker compose up -d --build
docker exec till-watch warp-cli --accept-tos status   # Connected
docker logs -f till-watch                              # „Проход: N каси, …“
```

Ако WARP не е свързан, проходът не се отчита. След 15 минути без отчет
лицензионният сървър показва касите като „неизвестно“, не „няма връзка“.

## Тестове

```
python3 -m pytest -q till-watch/tests
```
