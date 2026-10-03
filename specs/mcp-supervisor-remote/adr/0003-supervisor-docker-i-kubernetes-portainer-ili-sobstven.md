# ADR-0003: Супервайзорът управлява Docker и Kubernetes — през Portainer или собствен на Python

- **Статус:** **предложен** (03.10.2026) — анализ по искане на Росен; чака решение
- **Свързани:**
  - ADR-0001 (транспорт през Portainer exec);
  - ADR-0002 (sidecar exec зад Cloudflare);
  - ADR telegram-consult-bot/0009 (автоматична верига от поръчката до агента);
  - `odoo-rpc-mcp/klaudcho_provisioner.py` (3.3.14).
- **Искането:** „Искам супервайзорът да може да управлява и Kubernetes. За и против да минаваме през Portainer или да пишем самостоятелен супервайзор с Python API на Docker и Kubernetes. Доколко Portainer ще покрие всичко?“

## Какво трябва да може супервайзорът

1. Пуска, обновява и спира услуга от шаблон — стек в Docker или Deployment/StatefulSet в Kubernetes.
2. Работи с тайни, томове/PVC, мрежи, лимити за ресурси.
3. Следи състоянието: върви ли, здрав ли е, рестартира ли.
4. Дава логове и `exec` (за `supervisor.py`, `odoo shell -u`, ADR-0001).
5. Обновява версии с откат (rollout / rollback).
6. Стига до среди зад NAT, Cloudflare и VPN — нашите и на клиентите.
7. Има най-малки права: агентът на поръчката S00002 не може да пипа продуктивния Odoo.
8. Оставя одит — кой какво е пуснал.
9. Има стабилен договор — не се чупи при ъпгрейд на нещо чуждо.

## Какво имаме днес

| Среда | Как стигаме | Kubernetes? |
|---|---|---|
| poligroup (odoo-shell.dev, агентите) | SSH + Portainer CE 2.33 (`sts`) на :9443; `portainer.odoo-shell.dev` дава **525** зад Cloudflare (03.10) | не |
| odoo-dev-server (odoo-rpc) | SSH + Portainer | не |
| srv03 k3s (MEC, hg-lasser, odoo-mcp) | SSH, kubectl, k9s | **да**, k3s 1.29, 5 нода |
| клиенти: ussmed, mozu/ic-intracom, mebelfactor, MIV, konex, stedy | **само Portainer** (ADR-0001: „операторът е затворен в Portainer“) | смесено |

## Доколко Portainer покрива

| Нужда | Portainer CE | Бележка |
|---|---|---|
| 1. Стек от шаблон — Docker | ✅ `/api/stacks/create/standalone/string` | доказано в `provisioning_engine` |
| 1. Стек от шаблон — Kubernetes | ✅ `/api/stacks/create/kubernetes/string` (манифест или compose → k8s) | изисква k8s средата да е вързана в Portainer (агент или kubeconfig) |
| 2. Тайни, томове, мрежи, лимити | ✅ през самия манифест/compose | Portainer само го подава |
| 3. Състояние | ✅ Docker proxy `/api/endpoints/{id}/docker/...`; k8s proxy `/api/endpoints/{id}/kubernetes/...` | почти пълен Docker и k8s API, но **през посредник** |
| 4. Логове и exec | ✅ Docker; ⚠ k8s exec е през websocket на Portainer | ADR-0002: bodyless POST се чупи зад Cloudflare |
| 5. Обновяване и откат | ⚠ ре-деплой на стека; няма история и откат като `kubectl rollout undo` | откатът е наш код |
| 6. Зад NAT | ⚠ Edge Agent в CE е само интерактивен; **async режимът е само в Business Edition** | при прекъсваща връзка — BE |
| 7. Най-малки права | ❌ **CE има само „администратор“ и „стандартен потребител“**; фините роли и namespace-ограниченията в k8s са в BE | API ключът = цялата власт на потребителя над средата, включително продуктивния Odoo |
| 8. Одит | ❌ **логовете за действия са само в BE** | днес го пишем сами (`supervisor_deploy_ops.jsonl`) |
| 9. Стабилност | ⚠ пътищата за стекове са сменяни (`/api/stacks?type=…` → `/api/stacks/create/...`); ние сме на `sts` (кратка поддръжка) | всеки ъпгрейд на Portainer е риск |
| — Cloudflare | ❌ 525 сега; bodyless POST (ADR-0002) | Portainer зад Cloudflare вече ни спъва два пъти |

**Извод.** Portainer покрива **механиката** почти изцяло, и за Docker, и за Kubernetes. Не покрива **правата и одита** в CE. Добавя един посредник, зад който сме имали два отказа (Cloudflare). За точните права и одит трябва Business Edition — платен, на брой нодове.

## Собствен супервайзор на Python

**Как.** Малък агент на всеки хост или клъстер. **Изтегля задачите сам**, без входящ достъп — моделът на ErpNet.FP Fleet, доказан на живо. Под един интерфейс има два драйвера:

```
Runtime: apply(spec) · status(name) · logs(name) · exec(name, cmd) · delete(name) · rollback(name)
├── DockerRuntime      — docker SDK (docker-py), compose проекти с префикс
└── KubernetesRuntime  — kubernetes client, ServiceAccount + Role в ЕДИН namespace
```

| Нужда | Собствен | Бележка |
|---|---|---|
| 1–4 | ✅ директно API-то на Docker и k8s | без посредник |
| 5. Откат | ✅ k8s: история на ReplicaSet; Docker: предишният образ в нашия дневник | |
| 6. Зад NAT | ✅ агентът тегли задачите (HTTPS навън) | без Cloudflare за управлението |
| 7. Най-малки права — k8s | ✅ **вградено**: ServiceAccount с Role само в `klaudcho-*` | по-добре от Portainer BE |
| 7. Най-малки права — Docker | ⚠ `docker.sock` е root на хоста. Смекчава се с прокси на сокета (само позволените API пътища) и бял списък в кода (само проекти `klaudcho-*`) | Docker няма RBAC — валидно и за Portainer |
| 8. Одит | ✅ нашият дневник, плюс в Odoo (кой поръча, коя задача) | |
| 9. Стабилност | ✅ docker-py и kubernetes client следват стабилните API-та на Docker и k8s | договорът е наш |
| Цена | ❌ код за писане и поддръжка: шаблони, здраве, логове, exec, откат, обновяване на самия агент | ~ 3–5 дни за първа версия с двата драйвера |
| Инсталация | ❌ агент на всяка среда — при клиентите, достъпни само през Portainer, трябва тяхното съгласие | първото пускане може да стане **през Portainer** |

## Предложение — хибрид с един интерфейс

1. **Един интерфейс `Runtime` и три драйвера:**
   - `DockerRuntime` — docker-py;
   - `KubernetesRuntime` — kubernetes client;
   - `PortainerRuntime` — днешният код, зад същия интерфейс.

   Изпълнителят на Клаудчо и `supervisor_*` говорят само с интерфейса.
2. **Нашите среди** (poligroup, odoo-dev-server, srv03 k3s) — **собствен агент**, който тегли задачите:
   - k8s: ServiceAccount + Role само в своя namespace (агентите → `klaudcho`);
   - Docker: прокси на сокета + бял списък `klaudcho-*`;
   - без входящ порт, без Cloudflare за управлението.
3. **Средите на клиентите, до които стигаме само през Portainer** — `PortainerRuntime`, както днес. Където клиентът се съгласи, ги местим на собствения агент, като го пускаме веднъж **през Portainer**.
4. **Portainer остава** човешкият интерфейс — гледане, ръчни действия. Не е задължителен път за автоматиката.
5. **Odoo е източникът на задачите** (модел като `klaudcho.service`): кой, какво, кога, резултат, одит. Изпълнителят на Клаудчо се разделя:
   - Telegram стъпката остава в odoo-rpc, при единствения Telegram клиент;
   - стъпката за контейнера отива при агента на съответната среда.
6. **Ред:**
   1. `DockerRuntime` на poligroup — отблокира агентите на поръчките още сега;
   2. `KubernetesRuntime` на srv03 (namespace `klaudcho`);
   3. `PortainerRuntime` зад интерфейса;
   4. откат и обновяване на самия агент.

**Защо не само Portainer:**
- в CE няма права и одит;
- BE е платен, на брой нодове;
- Cloudflare вече ни спря два пъти;
- k8s RBAC е по-добър от ролите на Portainer BE.

**Защо не само собствен:**
- при клиентите често имаме само Portainer;
- Portainer е добър интерфейс за човека.

## Отворени въпроси (за Росен)

1. Хибридът ли, или чист собствен — с изискване всеки клиент да пусне агента?
2. Kubernetes — само srv03 k3s, или и клиентски клъстери?
3. Агентите на поръчките в Kubernetes или в Docker — къде трябва да живеят в бъдеще?
4. Portainer BE — има ли лиценз или интерес? Тогава правата и одитът от Portainer стават реални.
5. Нов образ `vladimirovrosen/supervisor-agent` с отделен ADR за договора „задача ↔ агент“?

## Източници

- Portainer CE срещу BE (права, одит, Edge async): https://www.portainer.io/blog/portainer-community-edition-ce-vs-portainer-business-edition-be-which-should-you-install · https://docs.portainer.io/admin/user/roles · https://docs.portainer.io/2.27/admin/environments/add/kubernetes/edge-async
- Стекове в Kubernetes през API: https://github.com/portainer/portainer-skills/blob/main/portainer-api/references/stacks.md
- Сменени пътища на API-то: https://oneuptime.com/blog/post/2026-08-02-portainer-stack-api-create-endpoints/view
