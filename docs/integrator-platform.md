# Integrator Platform (Track 3.x) — Overview

**Status:** Preview / in development.
**Branch:** `3.0` (docker tag `:next`, `:3.x.y`).
**Positioning:** Track 3.x turns `odoo-claude-mcp` from an **end-user AI tool**
into a **platform for Odoo integrators** — the partner, implementer, or agency
deploying Odoo + AI workflows for their own clients.

For the current end-user-oriented stack, see [`../README.md`](../README.md)
(track 2.x, `branch 2.0`, docker tags `:latest` / `:stable` / `:2.x.y`).

---

## Why a separate track?

The 2.x track ships the tools an Odoo **end-user** needs: CRUD, translations,
snippets, reports, AI tokenizer, memory. Those are the tools that a Bulgarian
SME, a Raytron accountant, or a bl-consulting content editor uses daily.

The 3.x track adds a layer **above** that — the tools an **integrator**
(Silver / Gold partner, OCA maintainer, agency) needs to stand up, configure,
and demo an entire Odoo + AI stack to *their own* clients, at scale.

---

## Four tracks (Track 3.x = this doc)

### Track 3.1 — Admin Lifecycle Tools

MCP tools for one-line control of a client's Odoo:

- `odoo_module_install(alias, modules, upgrade=False)` — resolves deps,
  orders installs, reports status per module.
- `odoo_module_upgrade(alias, modules | '*')`
- `odoo_module_uninstall(alias, module)` — with safety checks.
- `odoo_module_diff(alias, module)` — installed vs. latest repo version.
- `odoo_config_apply(alias, config_yaml)` — declarative
  `ir.config_parameter` / company settings / cron toggles.
- `odoo_backup_db(alias, target_path)` — pg_dump wrapper.
- `odoo_restore_db(alias, source_path, new_db_name)` — safe restore.
- `odoo_health_check(alias)` — version, module states, pending migrations,
  registry errors, recent ERROR log entries.

### Track 3.2 — Industry Skill Packs

Sellable bundles targeting a vertical or business model. Each pack =
Odoo modules + `ai.skill` records + `ai.pipeline.step` wiring + memory
packs + optional MCP plugin.

- **Manufacturing pack** — MRP routing, BoM matrix, quality, MPS/MTO,
  cost tracking, work-order automation.
- **Retail / POS pack** — POS session reconciliation, end-of-day close,
  cash reporting, fiscal device integration (Bulgaria НАП).
- **Services pack** — timesheet, project budget, recurring invoicing,
  retainer contracts, SLA tracking.
- **BG Localisation pack** — НАП export, VAT declarations, VIES,
  Intrastat, customs (DN+MRN), payroll.
- **AI Accounting Assistant pack** — OCR flow (vendor bill posting,
  receipt OCR, bank reconciliation prompts).

Pricing: €29–99 / pack / month, or bundled into BL subscription tiers.

### Track 3.3 — Demo Builder

One-command generator of a fresh demo environment:

- `mcp demo create --industry=manufacturing --seed=bg-ajika --client-name="Sofia Foods"`
  → creates tenant + Odoo DB + demo data + all pack skills + memory +
  pipeline steps.
- `mcp demo reset <tenant>` — wipe + reseed.
- `mcp demo list` — active demo tenants.
- `mcp demo export <tenant> --as-zip` — encrypted bundle for offline
  demos (AES-ZIP via pyzipper).
- Odoo web UI: "AI Billing → Demo Lab" menu with one-click industry
  buttons.

**Time-to-demo target:** < 5 minutes from "Client wants a demo" to "running
Odoo with their branding + AI-assisted flow".

### Track 3.4 — Module Dev + Test Toolkit

Claude CLI tools for writing + testing Odoo modules inside a session:

- `odoo_module_scaffold(alias, name, depends, models)` — generates
  `__manifest__.py` + `models/` skeleton + `views/` skeleton + access
  CSV from a structured prompt.
- `odoo_module_lint(path)` — pylint + odoo-specific rules.
- `odoo_module_test(alias, module, tags=[])` — run Odoo tests via RPC,
  stream results.
- `odoo_module_install_from_path(alias, path)` — upload local folder to
  Odoo addon path + install (dev iteration loop).
- `odoo_module_explain(alias, model_name)` — reflection: fields,
  relations, computed deps, sample records.
- `odoo_xml_validate(path)` — XML parse + access CSV coherence.

Positioning: "Claude Code for Odoo module development" — a niche the
current Odoo ecosystem doesn't have a polished tool for.

---

## Dependencies on existing work

**Already built (2.x):**

- Billing module (tenant, bundle, skill catalog, memory pack, Portainer
  client, AES-ZIP export).
- MCP pipeline engine with pluggable steps + Odoo-driven executor.
- OCR engine, `verify_ssl` cert pinning, licensed memory scope.
- Website snippet + translate tools.

**Re-used:**

- Existing Portainer stack deploy flow.
- Memory deployment audit, Anthropic Workspace automation.
- BG Trade Registry integration for partner bootstrap.

---

## Pricing tiers (planned)

| Buyer | Entry product | Upsell path |
|-------|---------------|-------------|
| Integrator / partner | BL Starter €49 (MCP + basic admin tools) | Industry packs as clients request |
| Integrator with 5+ clients | BL Business €129 + 2 packs | Full pack library + Demo Builder |
| Implementation agency | BL Professional €299 + Dev Toolkit | Co-branded marketplace presence |
| Agency selling own skills | BL Enterprise €599 | Revenue-share on marketplace |

---

## Anti-goals (explicitly not doing)

- **Not** a full Odoo hosting platform (Portainer + compose suffices).
- **Not** a low-code Odoo UI builder (Odoo Studio exists).
- **Not** an alternative to Odoo.sh for code deploy pipelines.
- **Not** a white-label reseller service for end-clients (BL fronts it).

---

## Roadmap

Track 3.x is parallelisable with ongoing 2.x feature work. Rough order:

1. Track 3.1 foundations (install / upgrade / diff / health-check) — ~5 days.
2. Track 3.3 demo builder MVP (manufacturing pack + Sofia Foods demo) —
   ~3 days after 3.1 unlocks install-from-template.
3. Track 3.2 first 2 skill packs (Manufacturing, BG Localisation) — ~4 days.
4. Track 3.4 dev toolkit — ~5 days.

**Total estimate:** ~3–4 weeks for tracks 3.1+3.3 MVP, ~6 weeks for full
four-track alpha.

---

## Status

| Track | Status | Docker tag |
|-------|--------|------------|
| 2.x End User | Production-stable | `:latest`, `:stable`, `:2.x.y` |
| 3.x Integrator | In development (preview only) | `:next`, `:3.x.y` |

For the full planning doc and open strategic questions, see project
memory `roadmap_integrator_platform.md`.
