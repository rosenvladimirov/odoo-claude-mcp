"""
v3 role-based tool security (USER / ADMIN / LEGACY).

Deny-by-list approach: explicit destructive tools are blocked for USER role.
Everything else passes (safer default than try-to-list-everything; the
classifier on odoo_sql_query already covers the SQL surface).

For odoo_unlink / odoo_execute(method=unlink) the gate inspects the model
argument and refuses if it's in PROTECTED_FROM_UNLINK.

Roles:
  admin  — no checks (v3 default)
  user   — destructive set blocked, protected_unlink blocked, elevation respected
  legacy — no checks (v2.x backwards compat for soft rollout window)

Override sets via env (CSV):
  MCP_USER_BLOCKED_TOOLS=...
  MCP_USER_BLOCKED_PROXY_PREFIXES=portainer__,filesystem__write
  MCP_PROTECTED_MODELS=res.company,res.users,...
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("tool_security")

DEFAULT_USER_BLOCKED_TOOLS: set[str] = {
    # system-exec
    "ssh_execute",
    "web_request",
    "odoo_web_call", "odoo_web_request", "odoo_web_export",
    "odoo_web_login", "odoo_web_logout", "odoo_web_read", "odoo_web_report",
    "git_remote",
    # bulk-destructive (custom Odoo helpers that wrap raw SQL)
    "odoo_stock_initial_delete",
    "odoo_stock_initial_import",
    "odoo_stock_initial_opening_journal",
    "odoo_stock_close_unaccounted_value",
    "odoo_stock_mo_delete_draft",
    "odoo_stock_product_flip_to_storable",
    # proxy meta-control
    "proxy_call",
    "proxy_discover",
    "proxy_refresh",
    # odoo_fp_* (firewall / fail-policy actions)
    "odoo_fp_configure",
    "odoo_fp_remove_action",
    # v3 provisioning admin tools (issue/revoke API keys)
    "provision_issue_api_key",
    "provision_revoke_api_key",
    "provision_list_api_keys",
}

DEFAULT_USER_BLOCKED_PROXY_PREFIXES: set[str] = {
    "portainer__",
    "filesystem__write_file",
    "filesystem__create_directory",
    "filesystem__delete",
    "filesystem__move",
    "oca__push",
    "ee__push",
    "backup__delete",
    "backup__archive_delete",
    "contabo__delete",
    "cloudflare__edit",
    "cloudflare__create",
    "cloudflare__delete",
}

DEFAULT_PROTECTED_FROM_UNLINK: set[str] = {
    # Tier 1 — system meta
    "res.company", "res.users", "res.groups",
    "ir.module.module", "ir.model", "ir.model.fields",
    "ir.model.access", "ir.cron", "ir.config_parameter",
    "ir.sequence", "ir.actions.server",
    # Tier 2 — accounting core
    "account.account", "account.journal",
    "account.tax", "account.tax.group", "account.fiscal.position",
    # Tier 3 — BG localization
    "l10n_bg.tax.office",
}

DEFAULT_PROTECTED_FROM_WRITE: set[str] = {
    # Tier 1 — system meta (write/create requires admin)
    "res.company", "res.users", "res.groups",
    "ir.module.module", "ir.model", "ir.model.fields",
    "ir.model.access", "ir.rule", "ir.cron",
    "ir.config_parameter", "ir.sequence", "ir.actions.server",
    # Tier 2 — auth/credential surface
    "ir.mail_server", "fetchmail.server",
    "auth.totp.user", "res.users.apikeys",
    # Tier 3 — accounting core (changes here require elevation)
    "account.account", "account.journal",
    "account.tax", "account.tax.group", "account.fiscal.position",
}

# Methods that require admin even if the model itself is unprotected.
# Explicit allowlist (preferred over substring scan — the latter
# false-positives on legitimate names like `pre_install_hook`,
# `_uninstall` callbacks, `action_install_check`).
#
# All known module-lifecycle methods on `ir.module.module` plus the
# `button_*` web-action variants. Models like `ir.module.module` are
# already in PROTECTED_FROM_WRITE — these blocks add a second gate
# for method-level invocation that does NOT touch fields directly.
DANGEROUS_METHOD_EXACT: set[str] = {
    # ir.module.module lifecycle
    "module_upgrade", "module_install", "module_uninstall",
    "module_download", "module_uninstall_module",
    "search_modules",
    # ir.module.module web-button variants (immediate = no wizard)
    "button_install", "button_upgrade", "button_uninstall",
    "button_immediate_install",
    "button_immediate_upgrade",
    "button_immediate_uninstall",
    # Method invocation chain — calling these via odoo_execute would
    # let a USER-role caller dispatch arbitrary methods bypassing the
    # model+method gate.
    "execute", "execute_kw",
    # Internal Odoo lifecycle hook — exposed as a method for some
    # registry-tampering tricks.
    "_unregister_hook",
}

UNLINK_TOOLS: set[str] = {"odoo_unlink"}
WRITE_TOOLS: set[str] = {"odoo_write"}
CREATE_TOOLS: set[str] = {"odoo_create"}


def _csv(env_name: str, default: set[str]) -> set[str]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return set(default)
    return {t.strip() for t in raw.split(",") if t.strip()}


USER_BLOCKED_TOOLS = _csv("MCP_USER_BLOCKED_TOOLS", DEFAULT_USER_BLOCKED_TOOLS)
USER_BLOCKED_PROXY_PREFIXES = _csv(
    "MCP_USER_BLOCKED_PROXY_PREFIXES", DEFAULT_USER_BLOCKED_PROXY_PREFIXES
)
PROTECTED_FROM_UNLINK = _csv("MCP_PROTECTED_MODELS", DEFAULT_PROTECTED_FROM_UNLINK)
PROTECTED_FROM_WRITE = _csv(
    "MCP_PROTECTED_WRITE_MODELS", DEFAULT_PROTECTED_FROM_WRITE
)


def get_role() -> str:
    """Returns 'admin' | 'user' | 'legacy'. Default depends on VERSION marker."""
    return os.environ.get("MCP_ROLE", "admin").strip().lower() or "admin"


def is_destructive(tool_name: str) -> bool:
    """True if the tool requires admin/elevated rights for USER role."""
    if tool_name in USER_BLOCKED_TOOLS:
        return True
    for prefix in USER_BLOCKED_PROXY_PREFIXES:
        if tool_name.startswith(prefix):
            return True
    return False


def is_protected_unlink(tool_name: str, arguments: dict | None) -> tuple[bool, str]:
    """For unlink tools, check if the target model is protected.

    Returns (is_blocked, model_name_or_empty)."""
    if tool_name not in UNLINK_TOOLS:
        # odoo_execute with method='unlink' is a separate concern handled
        # by the caller via is_protected_execute().
        return False, ""
    args = arguments or {}
    model = (args.get("model") or "").strip().lower()
    if model and model in PROTECTED_FROM_UNLINK:
        return True, model
    return False, model


def is_protected_execute(tool_name: str, arguments: dict | None) -> tuple[bool, str, str, str]:
    """For odoo_execute, refuse dangerous method+model combinations.

    Block conditions (any of):
      1. method=unlink on PROTECTED_FROM_UNLINK model
      2. method in {write, create} on PROTECTED_FROM_WRITE model
      3. method contains DANGEROUS_METHOD_SUBSTRINGS (upgrade/install/uninstall)
      4. method in DANGEROUS_METHOD_EXACT (execute, execute_kw, module_*)
      5. model=res.config.settings + method.startswith('execute')

    Returns (is_blocked, model, method, reason)."""
    if tool_name != "odoo_execute":
        return False, "", "", ""
    args = arguments or {}
    method = (args.get("method") or "").strip().lower()
    model = (args.get("model") or "").strip().lower()

    if method == "unlink" and model in PROTECTED_FROM_UNLINK:
        return True, model, method, "protected_unlink"

    if method in {"write", "create"} and model in PROTECTED_FROM_WRITE:
        return True, model, method, "protected_write"

    if method in DANGEROUS_METHOD_EXACT:
        return True, model, method, "dangerous_method_exact"

    if model == "res.config.settings" and method.startswith("execute"):
        return True, model, method, "config_settings_execute"

    return False, model, method, ""


def is_protected_write_create(tool_name: str, arguments: dict | None) -> tuple[bool, str, str]:
    """For odoo_write / odoo_create, refuse on protected models.

    Returns (is_blocked, model, op)."""
    if tool_name in WRITE_TOOLS:
        op = "write"
    elif tool_name in CREATE_TOOLS:
        op = "create"
    else:
        return False, "", ""
    args = arguments or {}
    model = (args.get("model") or "").strip().lower()
    if model in PROTECTED_FROM_WRITE:
        return True, model, op
    return False, model, op


def check_call(tool_name: str, arguments: dict | None,
               role: str | None = None,
               elevated: bool = False) -> tuple[bool, dict]:
    """Central gate. Returns (allowed, info_dict).

    info_dict on denial includes: reason, model (if applicable), hint.
    """
    if role is None:
        role = get_role()

    # admin and legacy bypass everything.
    if role in ("admin", "legacy"):
        return True, {"role": role, "bypass": True}

    # user role with active elevation: bypass.
    if elevated:
        return True, {"role": role, "elevated": True}

    # Refuse destructive tools.
    if is_destructive(tool_name):
        return False, {
            "reason": "destructive_tool",
            "tool": tool_name,
            "role": role,
            "hint": (
                "Tool requires admin rights. Use mcp_elevate(reason='...', "
                "ttl=300) to gain temporary elevation."
            ),
        }

    # Refuse protected unlink.
    blocked, model = is_protected_unlink(tool_name, arguments)
    if blocked:
        return False, {
            "reason": "protected_unlink",
            "tool": tool_name,
            "model": model,
            "role": role,
            "hint": f"Cannot unlink protected model '{model}'. Use mcp_elevate first.",
        }

    blocked, model, method, reason = is_protected_execute(tool_name, arguments)
    if blocked:
        return False, {
            "reason": f"protected_execute_{reason}",
            "tool": tool_name,
            "model": model,
            "method": method,
            "role": role,
            "hint": (
                f"Cannot execute method='{method}' on model='{model}' "
                f"(reason={reason}). Use mcp_elevate(reason='...', ttl=300) "
                f"to gain temporary admin rights."
            ),
        }

    blocked, model, op = is_protected_write_create(tool_name, arguments)
    if blocked:
        return False, {
            "reason": "protected_write_create",
            "tool": tool_name,
            "model": model,
            "op": op,
            "role": role,
            "hint": (
                f"Cannot {op} on protected model '{model}'. Use mcp_elevate "
                f"first or scope changes to non-protected models."
            ),
        }

    return True, {"role": role}


def filter_tools_for_role(tools: list, role: str | None = None,
                          elevated: bool = False) -> list:
    """Strip destructive tools from a Tool list for USER role."""
    if role is None:
        role = get_role()
    if role in ("admin", "legacy") or elevated:
        return tools
    return [t for t in tools if not is_destructive(t.name)]
