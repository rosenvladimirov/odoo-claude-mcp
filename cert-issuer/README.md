# cert-issuer — сертификатът на машините в магазините

Решение: ADR erpnet-kasi-fleet/0005 (`claude.ai/specs/erpnet-kasi-fleet/adr/`), Росен 29.10.2026.

Един сертификат за всички машини, от Let's Encrypt (DNS-01, без запис с адрес):
**`*.erpnet.mcpworks.net`** + по едно **`*.<клиент>.erpnet.mcpworks.net`** за всеки клиент с
записана машина — `*` покрива само едно ниво (RFC 6125), а машините са на две
(`kasa.<клиент>.erpnet.mcpworks.net`). Нов клиент ⇒ сървърът сам преиздава с името му. Сървърът сам го издава и подновява (под 30
дни); машините (проксито на касата) винаги го **дърпат** оттук през тунела на
mcpworks.net и го слагат пред устройствата си (`kasa.shop1.erpnet.mcpworks.net`). Токенът за DNS е само тук.

```
cert-issuer ──lego DNS-01──▶ Let's Encrypt
     ▲  GET /v1/cert (Bearer <тайната на машината>)  → chain + key
     └── проксито на касата ── през тунела на mcpworks.net (зад Access)
```

## Контрол

- Всяка машина е с тайна (`add-till`); пази се само sha256.
- Всяко теглене се записва: машина, кога, адрес (`CF-Connecting-IP`), сериен номер.
- `GET /` — таблица кой кога е теглил и дали има текущия сертификат; само за човек,
  пуснат от Cloudflare Access (`Cf-Access-Authenticated-User-Email`).
- `remove-till` спира машината веднага.

## Пускане

```
CERT_ISSUER_CF_DNS_TOKEN=…   # Cloudflare токен: Zone → Zone → Read + Zone → DNS → Edit, само за mcpworks.net
CERT_ISSUER_ACME_EMAIL=…
# staging по подразбиране; production — изрично:
CERT_ISSUER_ACME_SERVER=https://acme-v02.api.letsencrypt.org/directory
docker compose up -d cert-issuer
```

```
docker exec cert-issuer python /app/issuer.py add-till shop1/kasa1   # тайната се печата веднъж
docker exec cert-issuer python /app/issuer.py remove-till shop1/kasa1
docker exec cert-issuer python /app/issuer.py pulls
```

## Тестове

```
python3 -m pytest -q cert-issuer/tests
```
