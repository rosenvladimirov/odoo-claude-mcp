# Odoo RPC MCP Server — Bulgarian Localization Knowledge Base

## Session Context (auto-connect)

On startup, read `~/.odoo_session.json` for the Odoo instance that launched this terminal.
Check `odoo_connections` first — if a "default" connection already exists with the correct URL, use it.
If no connection exists, use `odoo_connect` with the session's url, db, username, protocol.
The user will provide the API key on first use — it is saved automatically for next time.
Only work with THIS Odoo connection. Do NOT connect to other instances.

## MCP Connection

- **HTTP**: `http://odoo-rpc-mcp:8084/mcp`
- **Health**: `http://odoo-rpc-mcp:8084/health`

## MCP Tools (21)

### Connection: `odoo_connect`, `odoo_disconnect`, `odoo_connections`
### Introspection: `odoo_list_models`, `odoo_fields_get`
### CRUD: `odoo_search`, `odoo_read`, `odoo_search_read`, `odoo_search_count`, `odoo_create`, `odoo_write`, `odoo_unlink`
### Advanced: `odoo_execute`, `odoo_report`, `odoo_version`
### View Refresh: `odoo_refresh`
### Fiscal Position: `odoo_fp_list`, `odoo_fp_details`, `odoo_fp_configure`, `odoo_fp_remove_action`, `odoo_fp_types`

All tools accept `connection` parameter (default: "default").

## IMPORTANT: Always refresh after mutations

After creating, updating, or deleting records, ALWAYS call `odoo_refresh` with the
model name so the user's Odoo browser tab auto-reloads:

```
odoo_create(model="sale.order", values={...})
→ odoo_refresh(model="sale.order")

odoo_write(model="res.partner", ids=[42], values={...})
→ odoo_refresh(model="res.partner", res_id=42)

odoo_execute(model="sale.order", method="action_confirm", args=[[42]])
→ odoo_refresh(model="sale.order", res_id=42)
```

This sends a bus notification to the user's browser. The list/form/kanban view
reloads automatically without page refresh.

---

## Bulgarian Localization Modules

### l10n_bg_config (Core)
Foundation module. Extends partners and companies with Bulgarian-specific fields.

**res.partner extensions:**
- `l10n_bg_uic` — UIC/BULSTAT number (company ID)
- `l10n_bg_egn` — EGN (personal ID number)
- `l10n_bg_pnf` — PNF (foreign person number)
- `l10n_bg_uic_type` — Selection: bg_uic, bg_egn, bg_pnf, eu_vat, other
- `l10n_bg_responsible_person_ids` — Many2many: responsible persons
- `l10n_bg_agent_ids` — Many2many: agents
- `l10n_bg_tax_agent_ids` — Many2many: tax agents

**account.move extensions:**
- `l10n_bg_name` — Official Bulgarian document number
- `l10n_bg_date` — Deal date (date of taxable event)
- `l10n_bg_narration` — Reason/narration for audit
- `l10n_bg_document_type` — Document type code

**Common operations:**
```
# Find partner by UIC
odoo_search_read model=res.partner domain=[["l10n_bg_uic","=","123456789"]] fields=["name","l10n_bg_uic","l10n_bg_uic_type","vat"]

# Get company BG config
odoo_search_read model=res.company domain=[] fields=["name","l10n_bg_uic","l10n_bg_responsible_person_ids"]
```

---

### l10n_bg_company_registry (Trade Registry)
Live API to portal.registryagency.bg. Search companies by EIK.

**Key method:** `res.partner` → `action_l10n_bg_check_registry`
```
# Search company in Trade Registry (triggers wizard)
odoo_execute model=res.partner method=action_l10n_bg_check_registry args=[[partner_id]]
```

---

### l10n_bg_city (Cities & EKATTE)
Complete Bulgarian settlement database with EKATTE codes.

**Models:**
- `l10n.bg.city.municipality` — Municipalities (265)
- `l10n.bg.city.cityhall` — City halls
- `res.city` — Settlements with `l10n_bg_ekatte` code

**Common operations:**
```
# Search city by EKATTE code
odoo_search_read model=res.city domain=[["l10n_bg_ekatte","=","68134"]] fields=["name","l10n_bg_ekatte","municipality_id","state_id"]

# List municipalities in a region
odoo_search_read model=l10n.bg.city.municipality domain=[["state_id.name","ilike","София"]] fields=["name","code"]

# Search settlement by name
odoo_search_read model=res.city domain=[["name","ilike","Пловдив"]] fields=["name","l10n_bg_ekatte","state_id","municipality_id"]
```

---

### l10n_bg_tax_admin (Tax Assistant)
VAT Protocols, Customs Declarations, Fiscal Position configuration.

**Models:**
- `account.fiscal.position.tax.action` — Tax action map (use `odoo_fp_*` tools)
- `account.move.bg.protocol` — VAT Protocol (Art. 117)
- `account.move.bg.private` — Private usage protocol
- `account.move.bg.customs` — Customs declaration

**account.move extensions:**
- `l10n_bg_move_type` — Selection: standard, customs, invoice_customs, private, protocol
- `l10n_bg_protocol_move_id` — Link to protocol
- `l10n_bg_private_move_id` — Link to private document
- `l10n_bg_customs_move_id` — Link to customs declaration

**account.move.bg.protocol fields:**
- `move_id` — Parent account.move
- `name` — Protocol number (auto-sequence)
- `date` — Protocol date
- State management: `action_post()`, `button_cancel()`, `button_draft()`

**account.move.bg.customs fields:**
- `move_id` — Parent account.move
- `declaration_number`, `declaration_date`
- `mrn` — Movement Reference Number (computed from components)
- `mrn_prefix`, `mrn_year`, `mrn_country_code`, `mrn_office_code`, `mrn_serial`
- `customs_office_id` — Many2one to `l10n.bg.customs.office`
- `country_of_origin_id`, `country_of_dispatch_id`
- `total_customs_value`, `total_expenses`, `total_gross_weight`
- `customs_procedure`, `transport_mode`, `incoterm_location`

**account.move.line extensions:**
- `l10n_bg_customs_value` — Customs value (computed)
- `l10n_bg_weight_gross`, `l10n_bg_weight_net`
- `l10n_bg_is_customs_expense` — Boolean
- `l10n_bg_personal_consumption`, `l10n_bg_total_consumption` — Private usage
- `l10n_bg_consumption_coefficient` — Computed ratio

**account.tax extensions:**
- Custom amount types: `customs_rate`, `private_rate`

**Fiscal position type_vat values:**
- `standard` — Accounting document
- `117_protocol_82_2` — (SER) Art. 117 — Art. 82, para. 2, item 3
- `117_protocol_84` — (ICD) Art. 117 — Art. 84
- `117_protocol_6_4` — (DON) Art. 117 — Art. 6, para. 4
- `117_protocol_6_3` — (PRIV) Art. 117 — Art. 6, para. 3
- `117_protocol_15` — (TRI) Art. 117 — Art. 15
- `117_protocol_82_2_2` — (TER) Art. 117 — Art. 82, para. 2, item 2
- `119_report` — Art. 119 - Report for sales
- `in_customs` — Import Customs declaration
- `out_customs` — Export Customs declaration

**Document types:** 01=Invoice, 02=Debit note, 03=Credit note, 07=Customs, 09=Protocols

**Common operations:**
```
# List protocols
odoo_search_read model=account.move.bg.protocol domain=[] fields=["name","date","move_id","state"] limit=20

# List customs declarations
odoo_search_read model=account.move.bg.customs domain=[] fields=["name","mrn","declaration_date","total_customs_value","customs_office_id"] limit=20

# Get customs offices
odoo_search_read model=l10n.bg.customs.office domain=[] fields=["name","code","city"]

# Find moves with protocol type
odoo_search_read model=account.move domain=[["l10n_bg_move_type","=","protocol"]] fields=["name","l10n_bg_name","partner_id","amount_total","state"] limit=20
```

---

### l10n_bg_reports_audit (Reports Foundation)
SQL-based reporting engine for Bulgarian VAT and audit reports.

**Key models:**
- Account tags for BG chart of accounts
- Report line configurations

**Common operations:**
```
# Get account tags for BG
odoo_search_read model=account.account.tag domain=[["applicability","=","taxes"],["country_id.code","=","BG"]] fields=["name","applicability"] limit=100
```

---

### l10n_bg_reports_config (Report Configuration)
VAT report lines, sale/purchase/VIES report configuration.

**Common operations:**
```
# Get VAT report configuration
odoo_search_read model=account.report domain=[["name","ilike","ДДС"]] fields=["name","line_ids"]
```

---

### l10n_bg_tariff_code (TARIC Codes)
EU TARIC/HS/CN code management with API integration.

**Models:**
- `l10n.bg.tariff.code` — Tariff codes with rates

**product.template extensions:**
- `l10n_bg_tariff_code_id` — Many2one to tariff code
- `l10n_bg_tariff_rate` — Tariff rate %
- `hs_code_id` — HS code reference

**Common operations:**
```
# Search TARIC codes
odoo_search_read model=l10n.bg.tariff.code domain=[["code","ilike","8541"]] fields=["code","name","rate"] limit=20

# Get product tariff info
odoo_search_read model=product.template domain=[["l10n_bg_tariff_code_id","!=",false]] fields=["name","l10n_bg_tariff_code_id","l10n_bg_tariff_rate","hs_code_id"] limit=20
```

---

### l10n_bg_tax_offices (NRA Offices)
Bulgarian NRA tax offices linked to cities.

**Model:** `l10n.bg.tax.office`

```
# List tax offices
odoo_search_read model=l10n.bg.tax.office domain=[] fields=["name","code","city_id"] limit=50
```

---

### l10n_bg_api_nra (NRA API)
Submit declarations to National Revenue Agency (НАП).

**Model:** `l10n.bg.api.nra.declaration`
- `declaration_type` — Type: decl1, decl6, etz, vat, vies
- `state` — draft, submitted, accepted, rejected
- `submission_date`, `response_code`, `response_message`

**Common operations:**
```
# List NRA declarations
odoo_search_read model=l10n.bg.api.nra.declaration domain=[] fields=["declaration_type","state","submission_date","response_code"] limit=20
```

---

### l10n_bg_infopay (Banking)
Bank statement sync via InfoPay API.

```
# List bank journals with InfoPay config
odoo_search_read model=account.journal domain=[["type","=","bank"]] fields=["name","bank_id","l10n_bg_infopay_enabled"] limit=10
```

---

### l10n_bg_erp_net_fp (Fiscal Printer)
ErpNet.FP fiscal printer integration for POS.

**pos.config extensions:**
- `l10n_bg_fiscal_printer_id`
- Fiscal receipt printing methods

---

### l10n_bg_hr_holidays (HR Leave Types)
61 pre-configured Bulgarian leave types: sick leave (NZOK codes 01-17), annual, maternity, educational.

```
# List Bulgarian leave types
odoo_search_read model=hr.leave.type domain=[] fields=["name","code","l10n_bg_code"] limit=70
```

---

### l10n_bg_payroll_classifications (NCOP/KID)
National Classification of Occupations (НКПД) and Economic Activities (КИД).

**Models:**
- `l10n.bg.ncop` — Occupation classification
- `l10n.bg.kid` — Economic activity classification

```
# Search occupation by code
odoo_search_read model=l10n.bg.ncop domain=[["code","ilike","2514"]] fields=["code","name"] limit=20

# Search economic activity
odoo_search_read model=l10n.bg.kid domain=[["code","ilike","62"]] fields=["code","name","mod_rate"] limit=20
```

---

### l10n_bg_bank_wallet (Crypto Keys)
Secure storage for API keys, certificates, RSA keys with PBKDF2+Fernet encryption.

**Model:** `l10n.bg.bank.wallet`
- `key_type` — rsa_key, api_key, password, certificate
- Encrypted storage with per-user salt

---

### partner_multilang (Multilingual Partners)
Automatic transliteration: Bulgarian ↔ Latin. JSONB storage for translations.

**res.partner extensions:**
- Translated fields stored in JSONB: name, street, city, company_name
- Auto-transliteration on create/write

```
# Search partner in any language
odoo_search_read model=res.partner domain=[["name","ilike","Росен"]] fields=["name","lang"] limit=10
```

---

### l10n_bg_report_theme (Report Theme)
Professional document theme with section-based layout, dual logos, dynamic colors.

**res.company extensions:**
- `l10n_bg_print_logo` — Print logo (separate from web logo)
- Report section configuration (header, article, footer)
- Background images per section

---

### l10n_bg_config_plugins_art_69_2 / art_82_2
VAT configuration plugins for EU cross-border transactions (Art. 69/82 ZDDS).

---

### taric_ai_classifier
AI-powered TARIC code classification using Claude API.

**product.template methods:**
- `action_classify_taric` — Classify product via AI
- `l10n_bg_taric_ai_suggestion` — AI-suggested TARIC code

---

## Enterprise Modules (l10n-bulgaria-ee)

### l10n_bg_hr_payroll (Payroll — 16 models)
Comprehensive Bulgarian payroll with salary rules, social security, NSSI integration.

**hr.contract extensions:**
- `l10n_bg_ncop_position_id` — NCOP classification (Many2one)
- `l10n_bg_contract_date` — Contract date
- `l10n_bg_contract_duration_type` — Duration type selection
- `l10n_bg_basic_leave_days` — Annual leave days (min 20)
- `l10n_bg_seniority_allowance_rate` — Seniority rate (min 0.6%)
- `l10n_bg_seniority_years` — Seniority years
- `l10n_bg_computed_seniority_allowance` — Computed seniority allowance
- `l10n_bg_other_permanent_allowances` — Other permanent allowances
- `l10n_bg_payment_frequency` — monthly, biweekly, weekly
- `l10n_bg_working_time_type` — full_time, part_time, flexible, summarized
- `l10n_bg_notice_period_days` — Notice period

**bg.hr.payroll.ncop.classification extensions:**
- Insurance rates computed: `doo_emp_1959_rate`, `doo_emp_1960_rate`, `doo_er_1959_rate`, `doo_er_1960_rate`, `upf_er_rate`, `upf_emp_rate`, `zo_emp_rate`, `zo_er_rate`, `ppf_rate`, `tzbp_rate`

**hr.leave.nssi.certificate:**
- `sick_leave_number`, `l10n_bg_egn`, `l10n_bg_uic`
- `date_from`, `date_to`, `leave_reason_code`
- `income_1` through `income_6` — Monthly income for 6 months
- `insured_months`, `insured_days`, `worked_days_period`
- `nssi_office_code`, `total_income`

**hr.payslip.nssi.declaration:**
- `insurance_type`, `worked_days`
- `insurance_start_day_1` through `insurance_start_day_5`
- `insurance_end_day_1` through `insurance_end_day_5`
- `average_daily_income_1` through `average_daily_income_5`

**l10n_bg.hr.contract.amendment:**
- `contract_id`, `amendment_number`, `amendment_type`
- `date_signed`, `date_effective`, `date_end`

**l10n_bg.nap.export.history:**
- `export_type`, `status`, `export_date`, `nap_reference`
- `export_xml`, `response_xml`, `error_message`
- Methods: `generate_nap_xml()`, `action_retry_export()`, `action_download_xml()`

```
# List employee contracts with BG fields
odoo_search_read model=hr.contract domain=[["state","=","open"]] fields=["employee_id","wage","l10n_bg_ncop_position_id","l10n_bg_seniority_years","l10n_bg_basic_leave_days","l10n_bg_working_time_type"] limit=20

# Get NSSI declarations for a payslip
odoo_search_read model=hr.payslip.nssi.declaration domain=[["payslip_id","=",123]] fields=["employee_id","insurance_type","worked_days"]

# List contract amendments
odoo_search_read model=l10n_bg.hr.contract.amendment domain=[["contract_id","=",45]] fields=["amendment_number","amendment_type","date_effective","subject"]

# NAP export history
odoo_search_read model=l10n_bg.nap.export.history domain=[["status","!=","success"]] fields=["export_type","status","export_date","error_message"] limit=20

# NSSI certificates (sick leave)
odoo_search_read model=hr.leave.nssi.certificate domain=[] fields=["sick_leave_number","l10n_bg_egn","date_from","date_to","leave_reason_code","total_income"] limit=20
```

---

### l10n_bg_assets (Tax Depreciation)
Bulgarian tax depreciation with freeze periods and dual depreciation boards.

**account.asset extensions:**
- `l10n_bg_depreciation_ids` — One2many: BG depreciation board lines
- `l10n_bg_freeze_period_ids` — One2many: freeze periods
- `l10n_bg_method_percentage` — Tax depreciation rate %
- `l10n_bg_tax_model_id` — Reference to tax model asset
- `l10n_bg_tax_model` — Boolean: is this a tax model
- `l10n_bg_disposal_date` — Disposal date

**bg.account.asset.depreciation.board:**
- `asset_id`, `sequence`, `line_date`, `ref`
- `method_percentage`, `original_value`, `salvage_value`
- `depreciation_value`, `value_residual`, `value`

```
# List assets with BG depreciation info
odoo_search_read model=account.asset domain=[["state","=","open"]] fields=["name","original_value","l10n_bg_method_percentage","l10n_bg_tax_model","l10n_bg_disposal_date"] limit=20

# Get BG depreciation board for an asset
odoo_search_read model=bg.account.asset.depreciation.board domain=[["asset_id","=",15]] fields=["sequence","line_date","original_value","depreciation_value","value_residual"] order="sequence"
```

---

### l10n_bg_vat_reports (VAT Declaration)
VAT declaration, sales/purchase ledgers, EC Sales List.

**Key handler:** `bg.account.report.vat.custom.handler`

```
# Generate VAT report data
odoo_execute model=bg.account.report.vat.custom.handler method=_get_results args=[] kwargs={"options":{"date":{"date_from":"2026-01-01","date_to":"2026-03-31"}}}
```

---

### l10n_bg_intrastat (Intrastat XML)
Bulgaria Intrastat XML declaration report.

**Key handler:** `account.intrastat.report.handler`
- Methods: `bg_intrastat_export_to_xml()`, `_bg_generate_xml_structure()`

```
# Get Intrastat report lines
odoo_search_read model=account.intrastat.code domain=[] fields=["code","name","type"] limit=50
```

---

### l10n_bg_customs_rate (Customs Currency Rates)
Currency rates from Bulgarian Customs Agency.

**Model:** `l10n_bg.customs.currency.rate`
- `currency_id`, `currency_code`, `rate`, `inverse_rate`, `scale`
- `date_start`, `date_end`
- Methods: `get_rate()`, `_fetch_rates_from_customs()`, `action_refresh_rates()`

```
# Get customs exchange rates
odoo_search_read model=l10n_bg.customs.currency.rate domain=[["currency_code","=","USD"]] fields=["currency_code","rate","date_start","date_end"] order="date_start desc" limit=10

# Refresh rates from Customs Agency
odoo_execute model=l10n_bg.customs.currency.rate method=action_refresh_rates args=[[]]
```

---

### l10n_bg_infopay_ui (InfoPay Banking UI)
Bank statement fetch and payment order submission via InfoPay.

**account.journal methods:**
- `action_infopay_fetch_data()` — Fetch bank statements
- `action_infopay_pull_payments()` — Pull payment status

**account.payment methods:**
- `action_infopay_submit()` — Submit payment
- `action_infopay_check_status()` — Check payment status

```
# Fetch bank statements for a journal
odoo_execute model=account.journal method=action_infopay_fetch_data args=[[journal_id]]

# Submit payment via InfoPay
odoo_execute model=account.payment method=action_infopay_submit args=[[payment_id]]
```

---

### l10n_bg_hr_contract_sign (Electronic Signatures)
Electronic signature for employment contracts via Odoo Sign.

**l10n_bg.hr.contract.amendment extensions:**
- `sign_request_ids` — Many2many to sign.request
- `sign_request_count` — Count of sign requests
- Methods: `open_sign_requests()`, `action_signature_request_wizard()`

---

### currency_rate_live_fix (BNB Rate Fix)
Fix currency rate download with correct BNB coefficient.

```
# Force update currency rates
odoo_execute model=res.company method=_update_currency_rate args=[[company_id]]
```

---

### l10n_bg_config_plugins_nsi_expences
NSI expense accounts (601.x, 602.x series) for Bulgarian chart of accounts.

### l10n_bg_config_plugins_payroll
Social security accounts: DOO, NZOK, DZPO, GVRS with NAP payment codes.

### l10n_bg_hr_payroll_account
Payroll accounting: Form 6 tax reporting.

**Model:** `account.hr.payroll.form.6` — Form 6 tax declaration data

### l10n_bg_reports_audit_assets
Asset reporting for Bulgarian audit.

### l10n_bg_sign_report_theme
User signatures in QWeb report templates.

### product_datasheets (EE Documents)
Product document management (certificates, datasheets).

**documents.document extensions:**
- `version`, `iso_number`, `date_issue`, `date_expiry`
- `notified_body_id`, `qc_manager_id`

```
# List product documents
odoo_search_read model=documents.document domain=[["res_model","=","product.template"]] fields=["name","version","iso_number","date_issue","date_expiry"] limit=20
```

---

## Multi-Company Workflow

IMPORTANT: Always confirm which company to work in before any data manipulation.
Use `connection` parameter or set company context:
```
# List companies
odoo_search_read model=res.company domain=[] fields=["id","name","l10n_bg_uic"]

# Read with company context
odoo_execute model=account.move method=search_read args=[[["state","=","posted"]]] kwargs={"fields":["name","amount_total"],"limit":10,"context":{"allowed_company_ids":[1]}}
```

## User MCP Config (from Odoo)

Module `l10n_bg_claude_terminal` stores per-user config in Preferences.
```
# Get full MCP config for current user
odoo_execute model=res.users method=get_claude_mcp_config args=[]
```
Returns: `terminal_url`, `odoo` (url, db, username, protocol), `telegram` (api_id, api_hash, phone, session_name), `viber` (bot_token, bot_name, webhook_url).

## Python Fallback

If MCP tools are unavailable, use xmlrpc.client directly:
```python
import xmlrpc.client
url, db, ak = "http://host.docker.internal:8069", "mydb", "api_key_here"
common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(db, "admin", ak, {})
obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
# Then: obj.execute_kw(db, uid, ak, "model.name", "method", [args], {kwargs})
```
