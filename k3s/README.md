# Odoo MCP — k3s (Rancher) / Kustomize deployment

K3s deployment на същия стак от `docker-compose.yml`, taргетиран към **Rancher-managed k3s клъстер**.
Манифестите са чист Kubernetes + Kustomize — работят еднакво през `kubectl` и през Rancher UI.

## Target платформа

- **k3s** (Rancher-provisioned, single- или multi-node)
- **Rancher 2.x** UI/CLI за workload management
- **Traefik v2** (бъндълнат в k3s) — използва се `traefik.containo.us/v1alpha1 IngressRoute`
- **local-path-provisioner** (default в k3s) — `storageClassName: local-path`

## Структура

```
k3s/
├── base/                   # всички ресурси (namespace, PVCs, 10 Deployments/Services, Ingress)
│   ├── namespace.yaml
│   ├── pvcs.yaml           # 5 PVC-та (local-path)
│   ├── configmaps.yaml     # proxy_services + claude templates (заменят се в overlay)
│   ├── secrets.example.yaml
│   ├── claude-terminal.yaml    (public)
│   ├── odoo-rpc-mcp.yaml       (public, gateway)
│   ├── portainer-mcp.yaml      (backend)
│   ├── teams-mcp.yaml          (backend)
│   ├── github-mcp.yaml         (backend)
│   ├── oca-mcp.yaml            (backend)
│   ├── ee-mcp.yaml             (backend)
│   ├── filesystem-mcp.yaml     (backend)
│   ├── qdrant.yaml             (backend)
│   ├── ollama.yaml             (backend)
│   ├── ingress.yaml            # Traefik IngressRoute за двата публични
│   └── kustomization.yaml
└── overlays/
    ├── prod/                       # Ingress + TLS (Cloudflare Tunnel или certResolver)
    │   ├── kustomization.yaml
    │   ├── ingress-patch.yaml      # смяна на hostnames
    │   └── .env.example
    └── direct/                     # БЕЗ Cloudflare — NodePort + plain HTTP
        ├── kustomization.yaml
        ├── nodeport-services.yaml  # NodePort 30080, 30084
        ├── patch-ingress-*.yaml    # маха TLS от base Ingress-ите
        ├── cert-manager-example.yaml   # опционален Let's Encrypt HTTP-01
        └── .env.example
```

## Мрежови tiers

- `tier: public` → claude-terminal, odoo-rpc-mcp → излагат се през Traefik Ingress.
- `tier: backend` → всичко останало → ClusterIP, достъпно само от pods в същия namespace.

Сегментация "public/backend" от docker-compose се пази чрез label `tier` и Ingress само за публичните. (За по-строго разделяне — добави `NetworkPolicy`, изисква CNI като Calico.)

## Volumes (local-path)

| PVC                | Size  | Mounted by                                         |
|--------------------|-------|----------------------------------------------------|
| `claude-user-data` | 20 Gi | claude-terminal (`/data/users`)                    |
| `mcp-shared-data`  | 5 Gi  | claude-terminal (`/shared-data`), odoo-rpc-mcp (`/data`) |
| `mcp-repos`        | 30 Gi | odoo-rpc-mcp, oca-mcp, ee-mcp, filesystem-mcp      |
| `qdrant-storage`   | 50 Gi | qdrant                                             |
| `ollama-data`      | 40 Gi | ollama                                             |

**Важно:** `mcp-shared-data` и `mcp-repos` се монтират от >1 pod (`ReadWriteOnce` работи само ако всички pods са на същия node — при multi-node setup трябва `ReadWriteMany` storage class или отделни PVC-та).

## Предпоставки

1. **k3s** — на един или повече nodes (`curl -sfL https://get.k3s.io | sh -`)
2. **Built images** на всички custom MCP-та в registry (ghcr.io или local). Override през `images:` в overlay.
3. **Traefik** — бъндълнат в k3s по подразбиране. Провери `kubectl -n kube-system get pods`.
4. **DNS** — насочи `terminal.example.com` и `mcp.example.com` към k3s node-а (или Cloudflare Tunnel към Service).

## Избор на overlay

| Overlay          | Кога                                                        | Достъп                                                     |
|------------------|-------------------------------------------------------------|------------------------------------------------------------|
| `overlays/prod/` | Имаш Cloudflare Tunnel ИЛИ public DNS + TLS cert resolver   | `https://terminal.example.com`, `https://mcp.example.com`  |
| `overlays/direct/` | БЕЗ Cloudflare — LAN/VPN/direct public IP достъп          | `http://<node-ip>:30080`, `http://<node-ip>:30084`         |

**`direct` без Cloudflare:**
- NodePort services — 30080 (claude-terminal), 30084 (odoo-rpc-mcp)
- Ingress патчнат на `entryPoints: [web]` (plain HTTP, без TLS)
- За TLS без Cloudflare → използвай `cert-manager-example.yaml` (cert-manager + Let's Encrypt HTTP-01, Rancher App catalog го има)
- Алтернатива: k3s Klipper LoadBalancer (коментар в `nodeport-services.yaml`) — слуша на 80/443 ако са свободни

## Deploy — вариант А: през `kubectl` (KUBECONFIG от Rancher)

Download KUBECONFIG-а от Rancher (Cluster → Kubeconfig File) → `~/.kube/config`.

Подмени `overlays/prod` с `overlays/direct` по-долу, ако не ползваш Cloudflare.

```bash
cd k3s/overlays/prod  # или overlays/direct

# 1. Попълни secrets
cp .env.example .env
nano .env

# 2. (По желание) копирай истински proxy_services.json и claude templates
# (Kustomize ще ги прочете от ../../../proxy_services.json и ../../../claude-terminal/)

# 3. Patch ingress hostnames
nano ingress-patch.yaml

# 4. Preview
kubectl kustomize --load-restrictor=LoadRestrictionsNone .

# 5. Apply
kubectl apply -k . --load-restrictor=LoadRestrictionsNone

# 6. Watch
kubectl -n odoo-mcp get pods -w
```

## Deploy — вариант Б: през Rancher UI

1. **Projects/Namespaces** → Create Namespace `odoo-mcp` и го асайнни към Rancher Project (примерно "MCP").
2. **Apps → Repositories** — ако държиш manifests в Git → добави repo-то като custom repo (не Helm).
   Или по-просто:
3. **More Resources → Cluster → ConfigMaps & Secrets** — импортирай `.env` стойностите ръчно като `Secret mcp-app` в `odoo-mcp`.
4. **Workloads → Import YAML** → paste output от `kubectl kustomize overlays/prod` и save.
5. Rancher визуализира всички Deployments, Services, Ingresses в UI.

За GitOps подход с Rancher → **Rancher Continuous Delivery (Fleet)**: добави `fleet.yaml` в корена на k3s/ папката, закачи Git repo към fleet-default → автоматичен sync.

## Build & push images

За да не използваш ghcr.io/rosenvladimirov/* placeholder-а:

```bash
# Пример — локален registry (k3s registry mirror)
docker build -t mylocal/claude-terminal:dev ./claude-terminal
docker push mylocal/claude-terminal:dev
```

После в `overlays/prod/kustomization.yaml`:

```yaml
images:
  - name: ghcr.io/rosenvladimirov/claude-terminal
    newName: mylocal/claude-terminal
    newTag: dev
```

## Post-deploy стъпки

```bash
# 1. Pull nomic-embed-text model в Ollama (еднократно)
kubectl -n odoo-mcp exec deploy/ollama -- ollama pull nomic-embed-text

# 2. Провери Qdrant
kubectl -n odoo-mcp port-forward svc/qdrant 6333:6333
curl http://localhost:6333/collections

# 3. Достъп до claude-terminal
kubectl -n odoo-mcp port-forward svc/claude-terminal 8080:8080
# или през Ingress на https://terminal.example.com
```

> `--load-restrictor=LoadRestrictionsNone` е нужен защото overlay-ите четат
> `proxy_services.json` и `claude-terminal/` от корена на repo-то (извън kustomization dir).

## Разлики спрямо docker-compose

| Feature                            | Docker Compose                     | k3s / Kustomize                        |
|------------------------------------|------------------------------------|----------------------------------------|
| Network segmentation               | `public`/`backend` bridge networks | Label `tier` + Ingress само за public  |
| SSH agent bridge                   | `${SSH_AUTH_SOCK}` bind            | Secret с SSH keys (`ssh-keys`)         |
| Cloudflare Tunnel                  | `cloudflare-net` external network  | Отделен `cloudflared` Deployment       |
| Host volumes (`${HOME}/...`)       | bind mounts                        | PVC-та (`local-path`)                  |
| Image builds                       | inline `build:`                    | pre-built + pushed в registry          |
| `.env` файл                        | автоматичен                        | `secretGenerator` от `.env`            |

## Rancher-специфични забележки

- **Project binding:** ако искаш всички ресурси да паднат в конкретен Rancher Project, добави анотация на Namespace-а:
  ```yaml
  metadata:
    annotations:
      field.cattle.io/projectId: "c-m-xxxxxxxx:p-yyyyyyyy"
  ```
- **Monitoring:** Rancher Monitoring stack (Prometheus + Grafana) автоматично scrape-ва Services с `prometheus.io/scrape: "true"` анотация. Добави я в overlay ако ти трябва observability на MCP портовете.
- **Logging:** Rancher Logging (Fluentd) хваща stdout на всички pods — няма нужда от допълнителна конфигурация.
- **Backup:** Rancher Backup Operator може да back-up-ва PVC-тата (`claude-user-data`, `qdrant-storage`, `ollama-data`).
- **RBAC:** Rancher Projects дават team-level достъп — в Rancher UI → Project Members.
- **Fleet (GitOps):** за multi-cluster deploy коммитни `k3s/` в Git и закачи като Fleet GitRepo.

## Fleet GitOps (ако ползваш Rancher Fleet)

`fleet.yaml` в корена на k3s/ (не е създаден по подразбиране — отключи при нужда):

```yaml
defaultNamespace: odoo-mcp
kustomize:
  dir: overlays/prod
```

## TODO за по-късно

- [ ] Cloudflare Tunnel Deployment (ако се изтегли public tier от Ingress)
- [ ] NetworkPolicy за backend tier (изисква Calico/Cilium CNI — k3s default flannel го НЕ поддържа)
- [ ] cert-manager за Let's Encrypt през Traefik (Rancher App catalog го има готов)
- [ ] HPA за claude-terminal ако стане multi-user heavy
- [ ] External Secrets / SealedSecrets вместо plain Secret
- [ ] Longhorn вместо local-path при multi-node Rancher клъстер (app catalog → longhorn)
- [ ] fleet.yaml + Rancher Fleet GitRepo binding
