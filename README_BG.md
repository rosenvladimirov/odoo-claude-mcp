# odoo-claude-mcp

Docker базиран мост между [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) и [Odoo](https://www.odoo.com/) чрез [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

Отваря уеб терминал с Claude Code и пълен RPC достъп до Odoo — търсене, четене, създаване, редакция, изтриване на записи, извикване на методи, генериране на отчети и конфигуриране на фискални позиции — всичко чрез естествен език.

---

## Архитектура

```
Internet / Cloudflare
  │
  ├─► claude-terminal (:8080)          ← уеб терминал, xterm.js + Claude Code
  │     │ MCP (HTTP)
  │     ▼
  └─► odoo-rpc-mcp (:8084)            ← GATEWAY, 60 native + 79 proxied = 139 tools
        │
        ├── filesystem-mcp (:8088)     ← plugin: файлове/папки, 14 tools
        ├── github-mcp     (:8086)     ← plugin: GitHub API, 26 tools
        ├── portainer-mcp  (:8085)     ← plugin: Docker mgmt, 39 tools
        └── teams-mcp      (:8087)     ← plugin: MS Teams, 6 tools
              │
              └── backend мрежа (без публичен достъп)
```

### Философия на плъгините

Проектът следва принципа **"една услуга — една отговорност"**:

- **Изолация**: Всеки плъгин е отделен Docker контейнер на backend мрежата. Няма публични портове, няма директен достъп от интернет. Ако плъгинът падне, останалите продължават да работят.
- **Единствена функция**: Filesystem MCP управлява файлове. GitHub MCP говори с GitHub API. Portainer MCP управлява Docker. Никой плъгин не прави повече от едно нещо.
- **Plug & Play**: Добавяне на нов плъгин = нов контейнер + един ред в `proxy_services.json`. Без промяна на код. Gateway-ят автоматично discover-ва tools при старт.
- **Самостоятелна работа**: Всеки плъгин може да се стартира и ползва независимо — директно по hostname:port, без gateway. Gateway-ят само агрегира и проксира.
- **Споделен storage**: Плъгините споделят Docker volumes за данни (repos, memory, connections). Всеки вижда само каквото му е mount-нато.

### Добавяне на нов плъгин

1. Стартирай MCP контейнер на backend мрежата (stdio + supergateway или native HTTP/SSE)
2. Добави един ред в `proxy_services.json`:
   ```json
   {"my-plugin": {"transport": "sse", "url": "http://my-plugin:PORT/sse"}}
   ```
3. Извикай `proxy_refresh` или рестартирай gateway-а
4. Готово — tools-ите на плъгина са достъпни на единния endpoint (:8084)

### Docker сървиси

| Сървис | Порт | Мрежа | Описание |
|--------|------|-------|----------|
| `claude-terminal` | 8080 | public | [ttyd](https://github.com/tsl0922/ttyd) уеб терминал, 19 теми, multi-user |
| `odoo-rpc-mcp` | 8084 | public + backend | MCP gateway — 60 native tools + proxy |
| `filesystem-mcp` | 8088 | backend | Файлови операции, sandboxed в `/repos` |
| `github-mcp` | 8086 | backend | GitHub repos, issues, PRs, code search |
| `portainer-mcp` | 8085 | backend | Docker container/stack management |
| `teams-mcp` | 8087 | backend | Microsoft Teams messaging |

---

## Бърз старт

### 1. Клониране и конфигурация

```bash
git clone https://github.com/rosenvladimirov/odoo-claude-mcp.git
cd odoo-claude-mcp
cp .env.example .env
```

Редактирай `.env`:

```bash
# Задължително: Anthropic API ключ за Claude Code
ANTHROPIC_API_KEY=sk-ant-...

# Опционално: предварително конфигурирана Odoo връзка
ODOO_URL=https://your-odoo.com
ODOO_DB=your_database
ODOO_USERNAME=admin
ODOO_API_KEY=your_odoo_api_key
ODOO_PROTOCOL=xmlrpc        # xmlrpc (Odoo 8+) или jsonrpc (Odoo 14+)
```

### 2. Стартиране

```bash
docker compose up -d --build
```

Това ще:
- Компилира ttyd от сорс (отнема 2-3 мин при първи build)
- Инсталира Claude Code CLI (`@anthropic-ai/claude-code`)
- Стартира MCP сървъра с Python 3.13
- Създаде споделен volume за credentials

### 3. Отваряне на терминала

Отвори **http://localhost:8080** в браузъра. Claude Code CLI е готов за ползване.

### 4. Свързване с Odoo

Ако си задал `ODOO_*` променливи в `.env`, връзката е автоматична. Иначе кажи на Claude:

```
Свържи се с моето Odoo на https://my-odoo.com, база "production",
потребител "admin", API ключ "abc123"
```

Claude ще извика `odoo_connect` и ще установи връзката. Credentials-ите се записват в `~/odoo-claude-connections/connections.json` и се помнят за следващи сесии.

---

## MCP инструменти — пълен списък

### Управление на връзки (3 инструмента)

| Инструмент | Описание | Параметри |
|------------|----------|-----------|
| `odoo_connect` | Добави или обнови именувана връзка | `alias`, `url`, `db`, `username`, `password`/`api_key`, `protocol` |
| `odoo_disconnect` | Премахни връзка по alias | `alias` |
| `odoo_connections` | Списък на всички активни връзки | — |

**Пример:**
```
Свържи се с production: https://erp.example.com, база "prod", user "admin", ключ "xxx"
```
Claude извиква: `odoo_connect(alias="production", url="https://erp.example.com", db="prod", username="admin", api_key="xxx")`

### Интроспекция (3 инструмента)

| Инструмент | Описание | Параметри |
|------------|----------|-----------|
| `odoo_version` | Версия на Odoo сървъра | `connection` (опционален) |
| `odoo_list_models` | Търсене на модели по pattern | `pattern`, `limit` |
| `odoo_fields_get` | Полета на модел (тип, string, relation) | `model`, `attributes` |

**Пример:**
```
Покажи ми всички модели свързани с фактури
```
Claude извиква: `odoo_list_models(pattern="invoice")`

### CRUD операции (7 инструмента)

| Инструмент | Описание | Параметри |
|------------|----------|-----------|
| `odoo_search` | Търсене → списък ID-та | `model`, `domain`, `limit`, `offset`, `order` |
| `odoo_read` | Четене по ID-та | `model`, `ids`, `fields` |
| `odoo_search_read` | Търсене + четене в едно | `model`, `domain`, `fields`, `limit`, `offset`, `order` |
| `odoo_search_count` | Брой записи | `model`, `domain` |
| `odoo_create` | Създаване (единичен или batch) | `model`, `values` (dict или list) |
| `odoo_write` | Обновяване | `model`, `ids`, `values` |
| `odoo_unlink` | Изтриване | `model`, `ids` |

**Примери:**
```
Покажи ми последните 10 неплатени фактури
→ odoo_search_read(model="account.move", domain=[["payment_state","=","not_paid"],["move_type","=","out_invoice"]], fields=["name","partner_id","amount_total","date"], limit=10, order="date desc")

Създай нов партньор "ACME Corp" с ДДС номер BG123456789
→ odoo_create(model="res.partner", values={"name": "ACME Corp", "vat": "BG123456789", "country_id": 22})

Колко са отворените поръчки за този месец?
→ odoo_search_count(model="sale.order", domain=[["state","=","sale"],["date_order",">=","2026-04-01"]])
```

### Разширени операции (2 инструмента)

| Инструмент | Описание | Параметри |
|------------|----------|-----------|
| `odoo_execute` | Извикване на произволен метод на модел | `model`, `method`, `args`, `kwargs` |
| `odoo_report` | Генериране на PDF отчет (base64) | `report_name`, `ids` |

**Примери:**
```
Потвърди поръчка SO-0042
→ odoo_execute(model="sale.order", method="action_confirm", args=[[42]])

Генерирай PDF за фактура INV/2026/0001
→ odoo_report(report_name="account.report_invoice", ids=[123])

Инсталирай модул l10n_bg_tax_admin
→ odoo_execute(model="ir.module.module", method="button_immediate_install", args=[[module_id]])
```

### Фискални позиции — българска локализация (5 инструмента)

Специализирани инструменти за конфигуриране на фискални позиции с tax action map. Проектирани за модула `l10n_bg_tax_admin`.

| Инструмент | Описание | Параметри |
|------------|----------|-----------|
| `odoo_fp_list` | Списък фискални позиции с брой mappings | `company_id`, `country_id`, `name` (филтри) |
| `odoo_fp_details` | Пълна конфигурация на ФП с всички tax action entries | `fp_id` |
| `odoo_fp_configure` | Добави/обнови tax action map запис | `fp_id`, `move_type`, `bg_move_type`, `type_vat`, `document_type`, `narration`, ... |
| `odoo_fp_remove_action` | Изтрий tax action map запис | `action_id` |
| `odoo_fp_types` | Справочни данни: налични стойности за типове | — |

**Примери:**
```
Покажи ми всички фискални позиции за компания "Моята Фирма"
→ odoo_fp_list(name="Моята Фирма")

Покажи детайлите за фискална позиция с ID 5
→ odoo_fp_details(fp_id=5)

Добави tax action map за протокол по чл. 117
→ odoo_fp_configure(fp_id=5, move_type="in_invoice", bg_move_type="protocol", type_vat="117_protocol_82_2", document_type="09", narration="ВОП стоки и услуги")
```

#### Справочни стойности за `odoo_fp_types`

**Типове документи (move_type):**
| Стойност | Описание |
|----------|----------|
| `out_invoice` | Изходяща фактура |
| `out_refund` | Изходящо кредитно известие |
| `in_invoice` | Входяща фактура |
| `in_refund` | Входящо кредитно известие |
| `entry` | Счетоводна операция |

**Български типове (bg_move_type):**
| Стойност | Описание |
|----------|----------|
| `standard` | Стандартен документ |
| `protocol` | Протокол по чл. 117 ЗДДС |
| `customs` | Митническа декларация |
| `private` | Документ за лично ползване |
| `invoice_customs` | Фактура с митнически документ |

**ДДС типове (type_vat):**
| Стойност | Описание |
|----------|----------|
| `standard` | Стандартна номерация |
| `in_customs` | Внос |
| `out_customs` | Износ |
| `117_protocol_82_2` | Протокол по чл. 82(2) |
| `117_protocol_84` | Протокол по чл. 84 |
| `119_report` | Отчет по чл. 119 |

---

## Множество връзки

Управлявай няколко Odoo инстанции по alias:

```
Свържи се с production: https://prod.example.com, база "prod", user "admin", ключ "xxx"
Свържи се с staging: https://staging.example.com, база "staging", user "admin", ключ "yyy"

Сравни броя партньори между production и staging
```

Всяка връзка се записва по име в `connections.json` и се помни между сесиите.

### CLI мениджър на връзки

`odoo_connect_cli.py` предоставя CLI управление (вътре в контейнера или локално):

```bash
# Списък на всички връзки
python odoo_connect_cli.py list

# Добавяне на нова връзка с тест
python odoo_connect_cli.py add production --url https://prod.example.com --db prod --user admin --api-key xxx --test

# Тест на съществуваща връзка
python odoo_connect_cli.py test production

# Изтриване (без потвърждение)
python odoo_connect_cli.py delete staging --yes

# Експорт на всички връзки (backup)
python odoo_connect_cli.py export backup.json

# Импорт
python odoo_connect_cli.py import backup.json

# SSH конфигурация
python odoo_connect_cli.py ssh-add production
python odoo_connect_cli.py ssh-test production
```

---

## Десктоп и CLI инструменти

Директорията `tools/` съдържа самостоятелни помощни програми, които допълват MCP сървъра. Изпълняват се на **хост машината** (не в Docker).

### Odoo Connection Manager (GUI)

GTK4/libadwaita десктоп приложение за визуално управление на Odoo връзки — в стил GNOME Settings със sidebar навигация.

```bash
# Зависимости: GTK4, libadwaita
pip install PyGObject
python tools/odoo_connect.py
```

**Възможности:**
- Добавяне, редакция, изтриване на Odoo връзки с визуален интерфейс
- Тестване на връзки (XML-RPC автентикация)
- SSH конфигурация за всяка връзка (хост, потребител, порт, метод на автентикация)
- Управлява `~/odoo-claude-connections/connections.json` — споделен с MCP сървъра
- Ползва `ODOO_CONNECTIONS_DIR` env var за промяна на пътя по подразбиране

### Odoo Module Analyzer

Анализира сорс кода на Odoo модул и генерира Claude-съвместим memory файл с XML-RPC примери.

```bash
# Зависимости
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# Анализ на един модул
python tools/odoo_module_analyzer.py /path/to/my_module

# Анализ на всички модули в repo
python tools/odoo_module_analyzer.py /path/to/repo --all-modules

# Задай друга изходна директория
python tools/odoo_module_analyzer.py /path/to/module --output ./memory/

# Само покажи кои файлове ще се изпратят (без API извикване)
python tools/odoo_module_analyzer.py /path/to/module --dry-run

# Анализирай конкретен модул от repo
python tools/odoo_module_analyzer.py /path/to/repo --module l10n_bg_tax_admin
```

**Какво генерира:**
- Описание на модула и ключови модели
- Готови за копиране XML-RPC операции (`search_read`, `create`, `write`, специфични методи)
- Бързи команди (trigger фрази)
- Зависимости и ограничения
- Записва резултата в `memory/module_<name>.md` и обновява `MEMORY.md` индекса

**Какво изпраща към Claude API:**
- `__manifest__.py`, `models/*.py`, `wizard/*.py`, `views/*.xml`, `security/ir.model.access.csv`
- Пропуска `static/`, `tests/`, `__pycache__/`, файлове > 80KB
- Общ лимит: 400KB на модул
- Модел: Claude Sonnet

### GLB 3D Viewer

GTK4 + OpenGL визуализатор на `.glb` (glTF 2.0 binary) 3D модели — полезен за инспекция на дизайн assets-и на продукти.

```bash
# Зависимости: GTK4, libadwaita, PyOpenGL, numpy, pygltflib
pip install PyGObject PyOpenGL numpy pygltflib
python tools/glb_viewer.py model.glb
```

**Възможности:**
- Въртене с мишка и zoom
- Автоматична цветова палета за mesh-ове без материали
- Поддържа positions, normals и indices

---

## Интеграция с Odoo (опционална)

Допълнителният Odoo модул **`l10n_bg_claude_terminal`** добавя AI бутон директно в Odoo:

- **Chatter**: Toggle бутон отваря терминален панел под формата
- **List View**: AI бутон в контролния панел отваря модален терминал

Модулът подава контекст на сесията (URL, база, потребител, текущ модел/запис) към терминала, така че Claude знае на кой запис гледаш.

**Как работи потокът:**
1. Потребителят натиска AI бутона
2. OWL компонентът вика `get_claude_mcp_config()` → получава URL/DB/username/protocol
3. Отваря iframe с ttyd URL + параметри: `?arg=ODOO_ORIGIN=https://...&arg=ODOO_DB=...`
4. `start-session.sh` парсва параметрите → пише `~/.odoo_session.json`
5. Claude Code стартира и чете сесийния контекст
6. При нужда извиква `/api/connect` за автоматична връзка

> Odoo модулът се поддържа отделно в [rosenvladimirov/l10n-bulgaria](https://github.com/rosenvladimirov/l10n-bulgaria).

---

## Конфигурация

### Променливи на средата

| Променлива | По подразбиране | Описание |
|------------|-----------------|----------|
| `ANTHROPIC_API_KEY` | | Anthropic API ключ за Claude Code |
| `TERMINAL_PORT` | `8080` | Порт на уеб терминала |
| `ODOO_MCP_PORT` | `8084` | Порт на MCP сървъра |
| `ODOO_URL` | | Предварително конфигуриран Odoo URL |
| `ODOO_DB` | | Предварително конфигурирана база данни |
| `ODOO_USERNAME` | | Предварително конфигуриран потребител |
| `ODOO_PASSWORD` | | Парола (или ползвай `ODOO_API_KEY` вместо нея) |
| `ODOO_API_KEY` | | Odoo API ключ (предпочитан пред парола) |
| `ODOO_PROTOCOL` | `xmlrpc` | `xmlrpc` (Odoo 8+) или `jsonrpc` (Odoo 14+) |
| `WORKSPACE_PATH` | `./_workspace` | Хост директория, mount-ната в `/workspace` |
| `CLAUDE_THEME` | `github` | Тема: github, dracula, monokai, solarized-light/dark, gruvbox-dark, atom, и още 12 |
| `SINGLE_CONNECTION` | `false` | Скрива connect/disconnect, ползва само "default" |

### Docker volumes

| Хост път | Контейнер път | Предназначение |
|----------|--------------|----------------|
| `~/.claude` | `/home/claude/.claude` | Claude Code конфигурация и памет |
| `~/.claude.json` | `/home/claude/.claude.json` | Claude login состояние |
| `~/odoo-claude-connections` | `/data` | Споделени credentials за връзки |
| `$WORKSPACE_PATH` | `/workspace` | Проектни файлове достъпни за Claude |

### Режим на единична връзка

Когато `ODOO_URL` е зададен или `SINGLE_CONNECTION=true`, MCP сървърът:

- Скрива `odoo_connect` и `odoo_disconnect` инструментите
- Ползва само предварително конфигурираната "default" връзка
- Идеален за embedded/iframe deployments, където връзката се задава от хост приложението

---

## Файлова структура

```
odoo-claude-mcp/
├── docker-compose.yml          # Оркестрация на всички сървиси
├── .env.example                # Шаблон за конфигурация
├── proxy_services.json         # Конфигурация на плъгините (gitignored)
├── proxy_services.json.example # Шаблон за плъгини
├── CHANGELOG.md                # История на промените
│
├── claude-terminal/            # Уеб терминал сървис
│   ├── Dockerfile              # Node 22 + ttyd (от сорс) + Claude Code CLI
│   ├── entrypoint.sh           # Стартира ttyd с тема от themes.json
│   ├── start-session.sh        # Auth → identify → symlink → theme → exec claude
│   ├── gateway.js              # Node.js reverse proxy (landing page + ttyd)
│   ├── landing.html            # Landing page при достъп без параметри
│   ├── themes.json             # 19 xterm.js цветови теми
│   ├── CLAUDE.md               # База от знания за Odoo
│   ├── settings.json           # Claude Code настройки
│   └── .mcp.json               # MCP endpoint (вътрешна Docker мрежа)
│
├── odoo-rpc-mcp/               # MCP gateway (главен сървис)
│   ├── Dockerfile              # Python 3.13-slim, non-root
│   ├── server.py               # 60 native tools + proxy engine
│   ├── google_service.py       # Gmail + Calendar OAuth2
│   ├── telegram_service.py     # Telegram клиент (Telethon)
│   ├── requirements.txt        # mcp, uvicorn, telethon, google-api, markdown
│   └── odoo_connect_cli.py     # CLI мениджър на връзки
│
├── filesystem-mcp/             # Плъгин: файлови операции
│   └── Dockerfile              # @modelcontextprotocol/server-filesystem + supergateway
│
├── portainer-mcp/              # Плъгин: Docker management
│   └── Dockerfile              # portainer-mcp binary + supergateway
│
├── teams-mcp/                  # Плъгин: Microsoft Teams
│   └── Dockerfile              # InditexTech mcp-teams-server + supergateway
│
└── tools/                      # Десктоп и CLI инструменти (хост машина)
    ├── odoo_connect.py         # GTK4 GUI мениджър на връзки
    ├── odoo_module_analyzer.py # Odoo модул → Claude memory файл
    └── glb_viewer.py           # GTK4 + OpenGL 3D GLB визуализатор
```

### Docker Hub Images

| Image | Описание |
|-------|----------|
| `vladimirovrosen/odoo-rpc-mcp` | MCP gateway (60 native tools) |
| `vladimirovrosen/odoo-claude-terminal` | Уеб терминал (ttyd + Claude Code) |
| `vladimirovrosen/odoo-filesystem-mcp` | Filesystem плъгин (14 tools) |
| `vladimirovrosen/odoo-github-mcp` | GitHub плъгин (26 tools) |
| `vladimirovrosen/odoo-portainer-mcp` | Portainer плъгин (39 tools) |

---

## Как работи вътрешно

### Поток на сесията

1. Потребителят отваря терминала (директно или чрез Odoo AI бутон)
2. `start-session.sh` чете URL параметрите (Odoo URL, DB, user, модел, ID на запис)
3. Контекстът на сесията се записва в `~/.odoo_session.json`
4. Claude Code CLI стартира с MCP конфигуриран към `odoo-rpc-mcp`
5. Claude чете сесийния контекст и автоматично се свързва с Odoo инстанцията
6. Потребителят взаимодейства с Odoo чрез естествен език

### MCP транспорт

MCP сървърът поддържа **Streamable HTTP** и **SSE** транспорти:

- **Контейнер към контейнер**: `http://odoo-rpc-mcp:8084/mcp` (Streamable HTTP)
- **Достъп от хоста**: `http://localhost:8084/mcp` или `http://localhost:8084/sse` (SSE)
- **Health check**: `http://localhost:8084/health`

### Съхранение на credentials

Връзките се записват в `~/odoo-claude-connections/connections.json`:

```json
{
  "default": {
    "url": "https://my-odoo.com",
    "db": "production",
    "user": "admin",
    "api_key": "...",
    "protocol": "xmlrpc"
  },
  "staging": {
    "url": "https://staging.example.com",
    "db": "staging",
    "user": "admin",
    "api_key": "...",
    "protocol": "xmlrpc",
    "ssh": {
      "host": "staging.example.com",
      "user": "odoo",
      "port": 22,
      "auth": "agent"
    }
  }
}
```

Файлът е споделен между двата контейнера чрез Docker volume mount.

---

## Сигурност

- **Не излагай порт 8080 в интернет** без автентикация. Терминалът дава пълен shell достъп.
- Credentials-ите се съхраняват на хост файловата система. Защити `~/odoo-claude-connections/` с подходящи права.
- И двата контейнера работят като non-root потребители (`claude` uid 1000, `mcp`).
- API ключовете са предпочитани пред пароли за Odoo автентикация.
- В production ползвай reverse proxy с TLS и автентикация (nginx + OAuth2, Traefik и др.).
- Никога не commit-вай `connections.json` — съдържа API ключове.

## Health Check

MCP сървърът експоузва health endpoint:

```bash
curl http://localhost:8084/health
# {"status": "ok", "connections": 1}
```

## Изисквания

- Docker и Docker Compose v2+
- Anthropic API ключ ([console.anthropic.com](https://console.anthropic.com))
- Odoo инстанция (версия 8+) с включен XML-RPC или JSON-RPC

## Управление

```bash
# Статус
docker compose ps

# Логове (на живо)
docker compose logs -f claude-terminal
docker compose logs -f odoo-rpc-mcp

# Рестарт
docker compose restart

# Спиране
docker compose down

# Спиране + изтриване на volumes (ВНИМАНИЕ: изтрива данни!)
docker compose down -v

# Rebuild (след промени в Dockerfile)
docker compose up -d --build
```

## Лиценз

[AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
