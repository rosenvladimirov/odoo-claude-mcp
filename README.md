<div align="center">

# Odoo Claude MCP

**Production-grade Model Context Protocol (MCP) server suite for Odoo ERP**

_Connect Claude, Claude Code, and any MCP-compatible client to Odoo, GitHub, filesystem, Portainer, Teams, and more — through a unified, authenticated gateway._

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Odoo](https://img.shields.io/badge/Odoo-15%20→%2019-714B67)](https://www.odoo.com)
[![MCP](https://img.shields.io/badge/MCP-Protocol-black)](https://modelcontextprotocol.io)
[![Docker](https://img.shields.io/badge/Docker-Compose%20%7C%20K3s-2496ED)](https://www.docker.com)
[![Made by BL Consulting](https://img.shields.io/badge/Made%20by-BL%20Consulting-714B67)](https://bl-consulting.net)

[Quick Start](#-quick-start) · [Architecture](#-architecture) · [MCP Servers](#-mcp-servers) · [Deployment](#-deployment) · [Claude.ai Connector](#-claudeai-connector) · [Documentation](#-documentation)

**[🇧🇬 Български README](README_BG.md)**

</div>

---

## 🎯 What is this?

`odoo-claude-mcp` is a **self-hosted MCP server suite** that turns any Odoo instance into a first-class citizen in the Claude ecosystem. It exposes Odoo data and operations through the Model Context Protocol, while bundling complementary MCP servers for everything an Odoo developer, consultant, or business user needs — GitHub, OCA modules, Kubernetes/Portainer, Microsoft Teams, and a full Claude Code terminal running in the browser.

Unlike single-purpose MCP wrappers, this stack is built for **real production use**:

- 🔐 **Unified authentication** across all MCP endpoints via token-based auth
- 🏢 **Multi-connection, multi-tenant** — one stack serves dozens of Odoo databases
- 🌐 **Claude.ai connector ready** — public HTTPS endpoint with token auth
- ☸️ **K3s / Kubernetes native** with Kustomize overlays for dev and prod
- 🐳 **Docker Compose** for solo developers and small teams
- 🖥️ **Web-based terminal** — xterm.js + tmux + Claude Code in the browser
- 📦 **Odoo module deployment** via direct RPC (no filesystem access needed)
- 🔍 **Qdrant vector store** integration for semantic search across records
- 🤖 **Ollama integration** for local LLMs and privacy-first deployments

---

## 🛤 Two Tracks — Which Branch Do You Want?

This project ships on **two parallel branches**, each targeting a different
audience. Pick the track that matches your role.

### 🧑‍💼 Track 2.x — End Users (current stable, `branch 2.0`)

**Who:** Odoo end-users, accountants, Bulgarian SMEs, developers who work
with a single Odoo stack, content teams managing website/blog.

**What you get:**

- All **188+ MCP tools** for day-to-day Odoo work (CRUD, search, RPC,
  introspection, attachments, reports, web session)
- **Multi-language field management** (`odoo_translate_field` +
  `odoo_translate_html` + 2 helpers — covers blog.post, product
  descriptions, website pages, arch_db) ★ new in 2.10
- **Website snippet management** (list / add / update / remove
  snippets on blog posts and pages, with background image swaps and
  substitutions) ★ new in 2.10
- **Bulgaria localization** (fiscal positions, VAT, НАП integration)
- **AI tokenizer** (Qdrant + Ollama embeddings per Odoo record)
- **Memory system** (shared + per-user + licensed memory packs)
- **Google / Telegram / Teams** integration
- **Claude.ai connector** — Bearer-token HTTPS endpoint ready

**Docker tags:** `:latest`, `:stable`, `:2.x.y` (current: **2.10.0**)

**Documentation:** this README

### 🔧 Track 3.x — Implementers / Integrators (preview, `branch 3.0`)

**Who:** Odoo implementation partners, OCA community contributors,
SaaS MSPs running multiple client instances, integrator agencies.

**What's planned (development — not production yet):**

- **Admin lifecycle tools** — `odoo_module_install/upgrade/uninstall/
  diff`, `odoo_config_apply`, `odoo_health_check`, `odoo_backup_db /
  restore_db`
- **Industry skill packs** — Manufacturing, Retail, Services, BG
  Localization, AI Accounting Assistant. Each pack = modules +
  `ai.skill` records + memory packs + pipeline steps.
- **Demo builder** — one-command generator of fresh demo
  environments (`mcp demo create --industry=... --seed=...`).
  Tenant + Odoo DB + demo data + skills + memory in < 5 minutes.
- **Module dev + test toolkit** — `odoo_module_scaffold / lint /
  test / install_from_path / explain`, `odoo_xml_validate`.

**Docker tags:** `:next`, `:3.x.y`

**Documentation:** [`docs/integrator-platform.md`](docs/integrator-platform.md)
(coming soon — see project memory `roadmap_integrator_platform.md`
for the full 4-track spec)

**Positioning:** the 3.x track shifts buyer persona from the final
Odoo user to the integrator / partner / agency — giving them the
tools to deploy, configure, and demo Odoo + AI workflows for their
own clients at scale.

---

## 🏗 Architecture

```
                        ┌──────────────────────────────┐
                        │   Claude.ai / Claude Code    │
                        │   Claude Desktop / IDE       │
                        └──────────────┬───────────────┘
                                       │ HTTPS + Token Auth
                                       ▼
        ┌──────────────────────────────────────────────────────────┐
        │              odoo-claude-mcp gateway                     │
        │  ┌────────────────────────────────────────────────────┐  │
        │  │  Unified MCP Router (server.py)                    │  │
        │  │  • Proxies to backend MCP servers                  │  │
        │  │  • Per-user profiles & connections                 │  │
        │  │  • Shared memory store                             │  │
        │  └────────────────────────────────────────────────────┘  │
        └──────────────────────────────────────────────────────────┘
                                       │
        ┌──────────────────────────────┴──────────────────────────────┐
        │                                                              │
   ┌────┴─────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌────────┴──┐
   │ odoo-rpc │  │  ee-mcp  │  │ oca-mcp  │  │ github-mcp│  │portainer- │
   │   -mcp   │  │ (Odoo EE)│  │   (OCA)  │  │           │  │    mcp    │
   └──────────┘  └──────────┘  └──────────┘  └───────────┘  └───────────┘
   ┌──────────┐  ┌──────────┐  ┌────────────────┐
   │filesystem│  │ teams-mcp│  │claude-terminal │
   │   -mcp   │  │          │  │  (xterm + tmux)│
   └──────────┘  └──────────┘  └────────────────┘
                                       │
                      ┌────────────────┼────────────────┐
                      ▼                ▼                ▼
                 ┌────────┐       ┌─────────┐      ┌─────────┐
                 │  Odoo  │       │ Qdrant  │      │ Ollama  │
                 │ 15-19  │       │  VDB    │      │  LLMs   │
                 └────────┘       └─────────┘      └─────────┘
```

---

## 🧰 MCP Servers

### Core: `odoo-rpc-mcp`

The flagship MCP server. **197+ MCP tools** (92 native + 105 proxied across Portainer, GitHub, Teams, EE, OCA, filesystem) covering every aspect of Odoo development and operations.

**Capabilities:**

- **CRUD & Search**: `odoo_search_read`, `odoo_create`, `odoo_write`, `odoo_unlink`, `odoo_execute`
- **Introspection**: `odoo_fields_get`, `odoo_list_models`, `odoo_module_info`
- **Multi-connection**: Switch between databases on the fly — `odoo_connect`, `user_connection_activate`
- **Web session support**: `odoo_web_login`, `odoo_web_call`, `odoo_web_export`, `odoo_web_report`
- **File operations**: `odoo_attachment_upload`, `odoo_attachment_download`, `public_access_download`
- **Reporting**: `odoo_report`, `public_access_report_pdf`, `public_access_report_xlsx`
- **Portal access**: `public_access_portal_orders`, `public_access_portal_invoices`, `public_access_portal_tickets`
- **Bulgaria l10n**: `odoo_fp_configure`, `odoo_fp_list`, `odoo_fp_details` — fiscal positions tailored for НАП compliance
- **Translations** ★ 2.10: `odoo_list_translatable_fields`, `odoo_get_field_translations`, `odoo_translate_field` (simple translate=True), `odoo_translate_html` (html_translate / xml_translate with `extract`/`terms`/`replace` modes). Version-aware (Odoo 16+ native JSONB API, <16 ir.translation fallback). Auto-ZWSP marks identical translations as "kept intentionally" so the website editor stops flagging them as untranslated.
- **Website snippets** ★ 2.10: `odoo_website_list_snippets`, `odoo_website_list_page_snippets`, `odoo_website_add_snippet`, `odoo_website_update_snippet`, `odoo_website_remove_snippet`. Lxml-based HTML parsing with xpath substitutions — covers blog posts, website pages (via arch_db), product descriptions, mega-menus. Supports background image swaps, CTA insertions, position-relative placement (end/begin/after/before/replace).
- **AI integration**: `ai_tokenize_record`, `ai_search_similar`, `ai_collection_info` — Qdrant vector embeddings per Odoo record
- **Memory system**: Per-user and shared memory with `memory_read`, `memory_write`, `memory_share`, `memory_pull`
- **Google services**: OAuth, Gmail search/read/send, Calendar CRUD
- **Telegram**: MTProto client — send messages, search contacts, read dialogs
- **SSH & Git**: Remote command execution, git operations

### `ee-mcp` — Odoo Enterprise Tools

Tools specific to Odoo Enterprise workflows:

- License validation & status checks
- EE module repository management
- Dependency analysis (CE + EE)
- Selective EE module linking into CE addons paths
- Conflict detection between Enterprise and OCA modules

### `oca-mcp` — OCA Module Management

Deep integration with the Odoo Community Association ecosystem:

- Clone individual OCA repos or `oca-clone-everything`
- Search across all local OCA repos
- Generate READMEs, icons, requirements.txt via `oca-gen-*`
- Version migration via `oca-migrate-branch`
- Changelog generation from newsfragments

### `claude-terminal` — Browser-based Claude Code

A complete **xterm.js + tmux + Claude Code** setup running in a Docker container:

- WebSocket gateway (`gateway.js`) with session isolation
- Per-user tmux sessions that persist across reconnects
- Themable terminal (see `themes.json` — includes Catppuccin, Dracula, Tokyo Night, Gruvbox, etc.)
- Landing page (`landing.html`) for public deployments
- Authenticated via the same token system as other MCP servers

### Infrastructure MCPs

- **`github-mcp`** — GitHub API wrapper (search code, issues, PRs, repos)
- **`portainer-mcp`** — Docker/Kubernetes environment management
- **`teams-mcp`** — Microsoft Teams messaging integration
- **`filesystem-mcp`** — Scoped filesystem operations for AI agents

---

## 📦 Companion Odoo Modules

The MCP stack is paired with two **Odoo modules** that live in the BG
localization repos. Together they turn any Odoo instance into a fully
MCP-aware, multi-tenant, billing-ready AI workstation.

### `l10n_bg_claude_terminal` — Odoo ↔ MCP integration (free / LGPL-3)

Odoo module that exposes MCP + Claude Terminal configuration as user
preferences and company settings. Works on **Odoo 16, 18 and 19** (each
major series has a dedicated branch).

**Repos:**

- **Odoo 18**: [`OCA/l10n-bulgaria` → `l10n_bg_claude_terminal`](https://github.com/rosenvladimirov/l10n-bulgaria/tree/18.0/l10n_bg_claude_terminal) · current: **18.0.1.28.0**
- **Odoo 19**: [`OCA/l10n-bulgaria` → `l10n_bg_claude_terminal`](https://github.com/rosenvladimirov/l10n-bulgaria/tree/19.0/l10n_bg_claude_terminal) · current: **19.0.1.24.0**
- **Odoo 16**: same repo, branch `16.0`

**What it adds to Odoo:**

- Per-user **MCP endpoint + Bearer token** config (Odoo UI → Preferences)
- Per-user **Odoo RPC connector** (URL, DB, API key, protocol,
  `verify_ssl` flag with TOFU cert pinning)
- Per-user **Web Session** credentials (for MCP's web session support)
- Per-user **Anthropic API key / OAuth token** passthrough (billing
  account or Claude Pro/Teams/Max)
- **Telegram + Viber** MTProto / bot token config
- **18 terminal themes** (Catppuccin, Dracula, Tokyo Night, Gruvbox, …)
- **Claude.ai OAuth login** button → one-click auth to Claude API
- **Live refresh bus** — MCP `odoo_create` / `odoo_write` triggers open
  form / list views to update in real time (no full reload)
- **Test Connections** button — smoke-tests Odoo RPC + MCP + Web Session
  + Qdrant + Ollama in one click (sticky notifications)
- **Save to MCP** button — register the user's Odoo alias into the MCP
  connection store without touching `/data/connections.json` manually
- **Dynamic XML-RPC db list** — populates the Database dropdown from
  the Odoo instance's `list_dbs()` (multi-tenant friendly)

### `l10n_bg_ai_billing` — SaaS billing module (OPL-1, paid)

Odoo module for hosting providers and BL Consulting tier management.
Tracks per-user MCP usage, calculates bills, provisions Portainer
stacks per tenant, ships licensed memory packs.

**Repo:**

- **Odoo 19**: [`OCA/l10n-bulgaria-expert` → `l10n_bg_ai_billing`](https://github.com/rosenvladimirov/l10n-bulgaria-expert/tree/19.0/l10n_bg_ai_billing) · current: **19.0.1.3.0**

**What it adds:**

- 8 models: `ai.billing.{bundle, tenant, usage.line, invoice.batch,
  skill.catalog, memory.pack, memory.deployment, tenant.addon}`
- **Bundle pricing** — Starter €49 / Business €129 / Professional €299
  / Enterprise €599 tiers with per-user / per-call / per-skill usage
  meters
- **Millicents precision** (\$0.00001) on usage lines — prevents the
  30–40% rounding loss common on cent-based billing
- **Portainer client wrapper** — `portainer.client` wizard creates a
  per-tenant MCP stack with auto-provisioned port, env, and network
- **AES-256 encrypted ZIP export** (via `pyzipper`) of tenant config
  bundles for offline demos or DR backups
- **Skill catalog** — `ai.skill` records with L1/L2/L3 disclosure tiers
- **Memory packs** — versioned markdown playbooks distributable to
  tenants via MCP `/admin/memory/upload` endpoint
- **BG Trade Registry integration** — fetches EIK / VAT / legal form
  from portal.registryagency.bg for tenant bootstrap
- **sale.order integration** — selling a bundle SKU auto-provisions
  the tenant + deploys memory + activates skills
- **MCP Terminal addons** — extra per-tenant features (dedicated
  subdomain, white-label branding)

**Dependency on the MCP stack:** uses the `MCP_ADMIN_TOKEN` endpoint
family (`/admin/memory/*`) added in `odoo-rpc-mcp` 2.8.0.

### Installation order

```
l10n_bg_claude_terminal   ← every user of the MCP terminal
       ↓
l10n_bg_ai_billing        ← hosting providers / resellers / BL-tier ops
```

`l10n_bg_ai_billing` depends on `l10n_bg_claude_terminal` — installing
the billing module auto-pulls the terminal integration.

---

## 🚀 Quick Start

### Option 1: Docker Compose (local dev)

```bash
git clone https://github.com/rosenvladimirov/odoo-claude-mcp.git
cd odoo-claude-mcp

# Configure
cp .env.example .env
nano .env                    # set ODOO_URL, DB, credentials, tokens

# Start the stack
docker compose up -d

# Verify
docker compose ps
curl http://localhost:8084/health
```

### Option 2: Quick installer script

**Linux / macOS:**

```bash
curl -fsSL https://raw.githubusercontent.com/rosenvladimirov/odoo-claude-mcp/main/install.sh | bash
```

**Windows (PowerShell as Administrator):**

```powershell
iwr -useb https://raw.githubusercontent.com/rosenvladimirov/odoo-claude-mcp/main/install.ps1 | iex
```

### Option 3: Connect to Claude Code

After the stack is running, add it to Claude Code:

```bash
claude mcp add odoo-mcp \
  --url https://your-domain.com/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

Or use the included `.mcp.json`:

```bash
cp claude-terminal/.mcp.json ~/.config/claude-code/mcp.json
```

---

## ☸️ Deployment

### Kubernetes / K3s (recommended for production)

Full Kustomize-based deployment in `k3s/`:

```
k3s/
├── base/                    # Base manifests
│   ├── namespace.yaml
│   ├── configmaps.yaml
│   ├── secrets.example.yaml
│   ├── pvcs.yaml
│   ├── odoo-rpc-mcp.yaml
│   ├── ee-mcp.yaml
│   ├── oca-mcp.yaml
│   ├── github-mcp.yaml
│   ├── teams-mcp.yaml
│   ├── portainer-mcp.yaml
│   ├── filesystem-mcp.yaml
│   ├── claude-terminal.yaml
│   ├── qdrant.yaml
│   ├── ollama.yaml
│   └── ingress.yaml
└── overlays/
    ├── direct/              # NodePort + cert-manager-example
    └── prod/                # Ingress with TLS for public endpoints
```

**Deploy:**

```bash
cd k3s/overlays/prod
cp .env.example .env
cp ../../base/secrets.example.yaml secrets.yaml
# edit secrets.yaml with real values

kubectl apply -k .
kubectl -n odoo-claude-mcp get pods
```

See [`k3s/README.md`](k3s/README.md) for complete deployment guide including cert-manager, Cloudflare tunnels, and horizontal scaling.

### Public deployment pattern

For Claude.ai connector access, the recommended production topology is:

```
Internet → Cloudflare (DNS + WAF) → Nginx reverse proxy → MCP gateway
                                                              │
                                                              ▼
                                                    Backend MCP servers
```

Token-based authentication on the gateway ensures only authorized Claude sessions connect. Cloudflare's Zero Trust or simple tunnel setup both work.

---

## 🔌 Claude.ai Connector

The stack is designed to be registered as a **Custom Connector** in Claude.ai (Team/Enterprise) or via the API.

**Configuration:**

1. Deploy the stack with a public HTTPS endpoint (e.g., `https://mcp.yourdomain.com`)
2. Generate a user token (see `odoo_connect_cli.py` or Qt GUI)
3. In Claude.ai Settings → Connectors → Add Custom Connector:
   - **URL**: `https://mcp.yourdomain.com/mcp`
   - **Auth**: Bearer token
4. The gateway will expose all MCP tools to your Claude conversations

**Security features:**

- Per-user profile isolation (`/data/users/{username}/`)
- Shared memory vs. personal memory separation
- Connection-level access control (users only see their own Odoo connections)
- All tool calls logged per user

---

## 🛠 Developer Tools

Beyond MCP servers, the repo includes standalone desktop and CLI tools:

### Connection Manager

**`tools/odoo_connect_qt.py`** — PyQt6 desktop GUI for managing Odoo connections, SSH keys, and MCP endpoints. Cross-platform (Linux/Windows/macOS).

**`tools/odoo_connect.py`** — GTK4/Adwaita alternative for Linux/GNOME users.

**`odoo-rpc-mcp/odoo_connect_cli.py`** — Terminal CLI for CI/CD and scripting.

### Module Analyzer

**`tools/odoo_module_analyzer.py`** — Analyzes Odoo module source for:

- Manifest validation
- Dependency graph extraction
- Model relationships
- View definitions
- Security rules

### GLB Viewer

**`tools/glb_viewer.py`** — 3D model inspection tool for the MRP Design Matrix workflows.

### Windows Installer

Pre-packaged NSIS installer (`packaging/windows/`) produced automatically via GitHub Actions (`.github/workflows/build-windows.yml`).

---

## 📚 Documentation

- **[README_BG.md](README_BG.md)** — Пълна документация на български
- **[CHANGELOG.md](CHANGELOG.md)** — Version history and release notes
- **[claude-terminal/CLAUDE.md](claude-terminal/CLAUDE.md)** — Claude Code workspace documentation
- **[k3s/README.md](k3s/README.md)** — Kubernetes deployment guide

---

## 🎨 Use Cases

### For Odoo Developers

- **Live module development** with Claude assisting directly on your running instance
- **RPC-based module deployment** — update code, views, data without filesystem access
- **Multi-environment workflows** — dev, staging, production from a single Claude session
- **OCA contribution flows** — clone, search, test, submit PRs through Claude

### For Odoo Consultants

- **Manage multiple client databases** from one authenticated session
- **Per-client memory** — Claude remembers context for each customer
- **Shared team knowledge** — `memory_share` distributes institutional know-how
- **НАП / Bulgaria localization** — built-in tools for fiscal positions, VAT compliance

### For Business Users

- **"Ask Claude about our sales data"** — natural language queries against real Odoo records
- **Document extraction workflows** — vision LLMs parse invoices into `account.move`
- **Semantic search** — find similar records, contracts, tickets across the whole database
- **Email & calendar integration** — Claude coordinates work across Odoo, Gmail, Calendar

### For Platform Operators (SaaS / MSP)

- **Multi-tenant hosting** — each client gets an isolated MCP endpoint
- **Billing integration** — usage tracking per tenant via Cloudflare AI Gateway
- **White-label terminals** — brand `claude-terminal` for your customers
- **Kubernetes scaling** — scale MCP replicas independently based on load

---

## 🔐 Security

- **No credentials in code** — all secrets via environment variables or Kubernetes secrets
- **Token-based MCP auth** — no shared passwords
- **Per-user data isolation** — filesystem and memory scoped to authenticated user
- **OAuth for third-party services** — Google, GitHub, Telegram all use standard OAuth flows
- **Connection encryption** — HTTPS/WSS everywhere in production deployments
- **Rate limiting** — via Cloudflare AI Gateway or ingress controller
- **Audit logging** — all MCP tool calls logged with user context

**Reporting security issues:** please email `vladimirov.rosen@gmail.com` rather than opening a public issue.

---

## 🌍 Bulgaria Localization

This project is maintained by the **[OCA `l10n-bulgaria`](https://github.com/OCA/l10n-bulgaria) maintainer**. Bulgarian-specific features are first-class:

- **НАП integration** — fiscal position tax action maps, VAT reports
- **`l10n_bg_*` module family** support — fiscal positions, VAT reports, payroll, HR
- **Образец 1** — monthly NAP declaration (Наредба №Н-13/2019)
- **Bulgarian partner identification** — UIC/ЕИК, legal forms, NACE activity codes
- **Transliteration** — BG ⇄ EN ⇄ GR mixin for partner names
- **НАП справка-декларация** — SQL-engine based audit reports

See the [Bulgaria-specific OCA modules](https://github.com/OCA/l10n-bulgaria) for the complete ecosystem.

---

## 🗺 Roadmap

- [ ] **Billing module** — native Odoo module for SaaS per-user MCP billing (in progress)
- [ ] **Multi-tenant dashboard** — admin UI for managing hosted MCP instances
- [ ] **Skills marketplace** — publish and subscribe to pre-built Odoo workflows
- [ ] **Invoice AI integration** — direct account.move extraction from attachments
- [ ] **Audit log UI** — searchable web UI for MCP tool call history
- [ ] **Self-healing connections** — automatic retry with token refresh on auth failures

---

## 🤝 Contributing

Contributions welcome! This project follows OCA conventions:

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/amazing-thing`)
3. Follow PEP 8 / Odoo coding guidelines
4. Add tests where applicable
5. Submit a PR with a clear description

For large changes, please open an issue first to discuss approach.

---

## 📜 License

This project is licensed under the **AGPL-3.0** license. See [LICENSE](LICENSE) for details.

---

## 🙏 Credits & Acknowledgements

- **[Anthropic](https://www.anthropic.com/)** — Claude, Claude Code, and the Model Context Protocol specification
- **[Odoo SA](https://www.odoo.com/)** — The ERP platform this project extends
- **[Odoo Community Association (OCA)](https://odoo-community.org/)** — The open-source Odoo ecosystem
- **[xterm.js](https://xtermjs.org/)**, **[tmux](https://github.com/tmux/tmux)** — Terminal layer
- **[Qdrant](https://qdrant.tech/)** — Vector database
- **[Ollama](https://ollama.com/)** — Local LLM inference

---

## 👤 Maintainer

**Rosen Vladimirov** — Founder, [BL Consulting](https://bl-consulting.net)
Odoo Silver Partner · OCA `l10n-bulgaria` maintainer · 10+ years of Odoo specialization

📧 Email: vladimirov.rosen@gmail.com
🐙 GitHub: [@rosenvladimirov](https://github.com/rosenvladimirov)
🏢 Company: Terraros Комерс ЕООД · Bulgaria

<div align="center">

---

**Made with ❤️ and ☕ in Bulgaria** 🇧🇬

_If this project helps you, consider [starring it on GitHub](https://github.com/rosenvladimirov/odoo-claude-mcp) ⭐_

</div>
