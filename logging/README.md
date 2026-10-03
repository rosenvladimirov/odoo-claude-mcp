# Логването: syslog → Alloy → Loki → S3

Решение на Росен, 03.10.2026: класически сървър за логове, на който стековете пращат сами
(драйвер `syslog` на Docker), а не копиране на файлове. ADR odoo-k8s-cluster/0001 т. 6а.

- **Alloy** на всяка машина слуша на `127.0.0.1:601` (TCP, RFC 5424) и препраща към Loki.
- **Loki** (централно, на poligroup, `127.0.0.1:3100`) пази в Contabo Object Storage, bucket `odoo-logs`, 30 дни.
- **Grafana** (https://grafana.mcpworks.net) → Explore → Loki.

## Стек към syslog (един по един, с пресъздаване на контейнерите)

```yaml
logging:
  driver: syslog
  options:
    syslog-address: "tcp://127.0.0.1:601"
    syslog-format: rfc5424
    tag: "{{.Name}}"
    mode: non-blocking
    max-buffer-size: 4m
```

`non-blocking` — контейнерът не спира, ако слушателят е паднал. Docker ≥ 20.10 пази и локален кеш
(„двойно логване“), затова `docker logs` и Portainer продължават да показват логовете.
Loki и Alloy НЕ се пращат през syslog — иначе логът на лога се затваря в кръг.

## Пускане

```
cp .env.example .env   # S3_*, LOKI_IMAGE, ALLOY_IMAGE, HOST, LOKI_URL
docker compose --profile central up -d   # машината с Loki
docker compose up -d alloy                 # другите машини
```
