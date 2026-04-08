"""
EE MCP Server — Odoo Enterprise Edition Module Management

Manages Enterprise addon repository with two modes:
- Direct: git operations in /opt/odoo/{version}/ee/ (self-hosted)
- Buffered: git operations in /repos/{instance}/ee/ (odoo.sh workflow)

Features: module management, license validation, OCA conflict detection.
"""
import ast
import glob
import json
import logging
import os
import subprocess
import sys
import xmlrpc.client
from typing import Any, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("ee-mcp")

BRANCH = os.environ.get("ODOO_BRANCH", "19.0")
DEFAULT_DIRECT_DIR = os.environ.get("EE_DIRECT_DIR", "/opt/odoo")
DEFAULT_BUFFER_DIR = os.environ.get("EE_BUFFER_DIR", "/repos")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
OCA_DIR_NAME = "oca"
EE_DIR_NAME = "ee"
EE_REPO = "odoo/enterprise"

server = Server("ee-mcp")

# ─── Known EE/OCA conflicts (name collisions) ────────────
# Modules that exist in BOTH EE and OCA — cannot coexist
KNOWN_CONFLICTS = {
    "account_asset", "account_budget", "account_followup",
    "account_batch_payment", "account_intrastat",
    "account_bank_statement_import", "account_bank_statement_import_ofx",
    "account_bank_statement_import_qif", "account_bank_statement_import_csv",
    "account_bank_statement_import_camt",
    "hr_payroll", "hr_payroll_account", "hr_payroll_holidays",
    "planning", "helpdesk", "quality_control",
    "stock_barcode", "sale_subscription",
}


# ─── Helpers ──────────────────────────────────────────────

def _resolve_ee_dir(args: dict) -> str:
    """Resolve EE working directory based on mode."""
    mode = args.get("mode", "direct")
    instance = args.get("instance", "")
    branch = args.get("branch", BRANCH)

    if mode == "buffered" and instance:
        base = os.path.join(DEFAULT_BUFFER_DIR, instance, EE_DIR_NAME)
    else:
        base = os.path.join(DEFAULT_DIRECT_DIR, f"odoo-{branch}", EE_DIR_NAME)

    return base


def _resolve_oca_dir(args: dict) -> str:
    """Resolve OCA directory for conflict detection."""
    mode = args.get("mode", "direct")
    instance = args.get("instance", "")
    branch = args.get("branch", BRANCH)

    if mode == "buffered" and instance:
        return os.path.join(DEFAULT_BUFFER_DIR, instance, OCA_DIR_NAME)
    return os.path.join(DEFAULT_DIRECT_DIR, f"odoo-{branch}", OCA_DIR_NAME)


def _run(cmd: list[str], cwd: str = None, timeout: int = 300) -> dict:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"Timeout after {timeout}s"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def _scan_modules(path: str) -> list[dict]:
    """Scan directory for Odoo modules."""
    modules = []
    if not os.path.isdir(path):
        return modules

    for entry in sorted(os.listdir(path)):
        manifest_path = os.path.join(path, entry, "__manifest__.py")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = ast.literal_eval(f.read())
            modules.append({
                "name": entry,
                "summary": data.get("summary", data.get("description", ""))[:120],
                "version": data.get("version", ""),
                "license": data.get("license", ""),
                "depends": data.get("depends", []),
                "auto_install": data.get("auto_install", False),
                "installable": data.get("installable", True),
                "category": data.get("category", ""),
                "application": data.get("application", False),
                "countries": data.get("countries", []),
            })
        except Exception:
            continue
    return modules


def _find_ee_repo(ee_dir: str) -> str:
    """Find enterprise repo directory (might be ee_dir/enterprise/ or ee_dir itself)."""
    enterprise_sub = os.path.join(ee_dir, "enterprise")
    if os.path.isdir(os.path.join(enterprise_sub, ".git")):
        return enterprise_sub
    if os.path.isdir(os.path.join(ee_dir, ".git")):
        return ee_dir
    return enterprise_sub  # default expected path


def _parse_inherits(module_path: str) -> set[str]:
    """Parse _inherit declarations from Python files in a module."""
    inherits = set()
    if not os.path.isdir(module_path):
        return inherits

    for py_file in glob.glob(os.path.join(module_path, "models", "*.py")):
        try:
            with open(py_file, "r", encoding="utf-8") as f:
                content = f.read()
            # Simple regex-free parsing for _inherit = "model.name" or _inherit = ["m1", "m2"]
            for line in content.split("\n"):
                line = line.strip()
                if "_inherit" in line and "=" in line:
                    _, _, value = line.partition("=")
                    value = value.strip().strip("'\"[]")
                    for v in value.split(","):
                        v = v.strip().strip("'\" ")
                        if v and "." in v:
                            inherits.add(v)
        except Exception:
            continue
    return inherits


# ─── Tool definitions ─────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ── Module management ──
        Tool(
            name="ee_clone",
            description="Clone Odoo Enterprise repository with GitHub token authentication.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                    "token": {"type": "string", "default": "", "description": "GitHub token (overrides env)"},
                },
            },
        ),
        Tool(
            name="ee_update",
            description="Git pull Enterprise repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
            },
        ),
        Tool(
            name="ee_modules",
            description="List all Enterprise modules with category, license, depends. Filter by category, country, or keyword.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "default": "", "description": "Filter by name/category keyword"},
                    "category": {"type": "string", "default": "", "description": "Filter by category"},
                    "country": {"type": "string", "default": "", "description": "Filter by country code (e.g. 'bg')"},
                    "apps_only": {"type": "boolean", "default": False, "description": "Show only application modules"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
            },
        ),
        Tool(
            name="ee_search",
            description="Search Enterprise modules by name, summary, or dependency.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="ee_link",
            description="Create symlink for specific EE modules in the Odoo addons path (selective install).",
            inputSchema={
                "type": "object",
                "properties": {
                    "modules": {"type": "array", "items": {"type": "string"}, "description": "Module names to link"},
                    "target_dir": {"type": "string", "default": ""},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["modules"],
            },
        ),
        Tool(
            name="ee_unlink",
            description="Remove symlinks for EE modules from addons path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "modules": {"type": "array", "items": {"type": "string"}, "description": "Module names to unlink"},
                    "target_dir": {"type": "string", "default": ""},
                    "branch": {"type": "string", "default": BRANCH},
                },
                "required": ["modules"],
            },
        ),
        Tool(
            name="ee_depends",
            description="Show full dependency tree of an EE module (CE + EE deps).",
            inputSchema={
                "type": "object",
                "properties": {
                    "module": {"type": "string", "description": "Module name"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["module"],
            },
        ),
        Tool(
            name="ee_deploy",
            description="Deploy EE from buffer to target (rsync). Buffered mode only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string", "description": "Instance name"},
                    "target": {"type": "string", "description": "Target path"},
                    "branch": {"type": "string", "default": BRANCH},
                },
                "required": ["instance", "target"],
            },
        ),
        # ── License/access ──
        Tool(
            name="ee_token_check",
            description="Validate GitHub token — check access to odoo/enterprise private repo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "token": {"type": "string", "default": "", "description": "GitHub token (overrides env)"},
                },
            },
        ),
        Tool(
            name="ee_license_status",
            description="Read Enterprise license status from an Odoo instance (expiration, code, reason).",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Odoo URL"},
                    "db": {"type": "string", "description": "Database name"},
                    "username": {"type": "string", "default": "admin"},
                    "api_key": {"type": "string", "description": "API key or password"},
                },
                "required": ["url", "db", "api_key"],
            },
        ),
        # ── OCA conflict detection ──
        Tool(
            name="ee_oca_conflicts",
            description="Detect conflicts between Enterprise and OCA modules: name collisions, model overlaps.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
            },
        ),
        Tool(
            name="ee_oca_recommend",
            description="For a module that exists in both EE and OCA — compare and recommend which to use.",
            inputSchema={
                "type": "object",
                "properties": {
                    "module": {"type": "string", "description": "Module name"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["module"],
            },
        ),
    ]


# ─── Tool handlers ─────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = _handle_tool(name, arguments)
    except Exception as e:
        result = {"error": str(e)}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


def _handle_tool(name: str, args: dict) -> Any:
    ee_dir = _resolve_ee_dir(args)
    branch = args.get("branch", BRANCH)

    # ── Clone ──
    if name == "ee_clone":
        token = args.get("token") or GITHUB_TOKEN
        if not token:
            return {"error": "GitHub token required for Enterprise repo access"}

        repo_path = _find_ee_repo(ee_dir)
        os.makedirs(ee_dir, exist_ok=True)

        if os.path.isdir(os.path.join(repo_path, ".git")):
            r = _run(["git", "pull"], cwd=repo_path)
            return {"status": "ok" if r["returncode"] == 0 else "error", "action": "updated", "path": repo_path}

        url = f"https://oauth2:{token}@github.com/{EE_REPO}.git"
        r = _run(["git", "clone", "--branch", branch, url], cwd=ee_dir, timeout=600)
        return {
            "status": "ok" if r["returncode"] == 0 else "error",
            "action": "cloned",
            "path": repo_path,
            "output": r["stderr"] if r["returncode"] != 0 else "",
        }

    # ── Update ──
    elif name == "ee_update":
        repo_path = _find_ee_repo(ee_dir)
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            return {"error": f"Enterprise repo not found at {repo_path}. Run ee_clone first."}
        r1 = _run(["git", "fetch", "--all"], cwd=repo_path)
        r2 = _run(["git", "pull"], cwd=repo_path)
        return {"status": "ok" if r2["returncode"] == 0 else "error", "path": repo_path, "output": r2["stdout"]}

    # ── Modules list ──
    elif name == "ee_modules":
        repo_path = _find_ee_repo(ee_dir)
        modules = _scan_modules(repo_path)

        filter_kw = args.get("filter", "").lower()
        category = args.get("category", "").lower()
        country = args.get("country", "").lower()
        apps_only = args.get("apps_only", False)

        if filter_kw:
            modules = [m for m in modules if filter_kw in m["name"] or filter_kw in m.get("summary", "").lower()]
        if category:
            modules = [m for m in modules if category in m.get("category", "").lower()]
        if country:
            modules = [m for m in modules if country in m.get("countries", [])]
        if apps_only:
            modules = [m for m in modules if m.get("application")]

        return {"total": len(modules), "modules": modules}

    # ── Search ──
    elif name == "ee_search":
        repo_path = _find_ee_repo(ee_dir)
        query = args["query"].lower()
        modules = _scan_modules(repo_path)
        results = [
            m for m in modules
            if query in m["name"]
            or query in m.get("summary", "").lower()
            or query in m.get("category", "").lower()
            or any(query in d for d in m.get("depends", []))
        ]
        return {"query": query, "found": len(results), "modules": results[:50]}

    # ── Link ──
    elif name == "ee_link":
        repo_path = _find_ee_repo(ee_dir)
        target_dir = args.get("target_dir") or f"/var/lib/odoo/.local/share/Odoo/addons/{branch}"
        os.makedirs(target_dir, exist_ok=True)

        linked = []
        errors = []
        for module_name in args["modules"]:
            source = os.path.join(repo_path, module_name)
            if not os.path.isfile(os.path.join(source, "__manifest__.py")):
                errors.append({"module": module_name, "error": "not found in Enterprise"})
                continue

            link = os.path.join(target_dir, module_name)
            if os.path.islink(link):
                os.unlink(link)
            os.symlink(source, link)
            linked.append(module_name)

        return {"linked": linked, "errors": errors}

    # ── Unlink ──
    elif name == "ee_unlink":
        target_dir = args.get("target_dir") or f"/var/lib/odoo/.local/share/Odoo/addons/{branch}"
        unlinked = []
        for module_name in args["modules"]:
            link = os.path.join(target_dir, module_name)
            if os.path.islink(link):
                os.unlink(link)
                unlinked.append(module_name)
        return {"unlinked": unlinked}

    # ── Depends ──
    elif name == "ee_depends":
        repo_path = _find_ee_repo(ee_dir)
        module_name = args["module"]
        modules = {m["name"]: m for m in _scan_modules(repo_path)}

        if module_name not in modules:
            return {"error": f"Module '{module_name}' not found in Enterprise"}

        # BFS dependency tree
        visited = set()
        queue = [module_name]
        tree = {}
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            mod = modules.get(current)
            if mod:
                deps = mod.get("depends", [])
                tree[current] = {"depends": deps, "is_ee": True, "license": mod.get("license", "")}
                queue.extend(d for d in deps if d not in visited)
            else:
                tree[current] = {"depends": [], "is_ee": False, "license": "CE/unknown"}

        ee_deps = [k for k, v in tree.items() if v["is_ee"] and k != module_name]
        ce_deps = [k for k, v in tree.items() if not v["is_ee"]]

        return {
            "module": module_name,
            "total_deps": len(tree) - 1,
            "ee_deps": ee_deps,
            "ce_deps": ce_deps,
            "tree": tree,
        }

    # ── Deploy ──
    elif name == "ee_deploy":
        instance = args["instance"]
        target = args["target"]
        source = _find_ee_repo(os.path.join(DEFAULT_BUFFER_DIR, instance, EE_DIR_NAME))

        if not os.path.isdir(source):
            return {"error": f"Buffer not found: {source}"}

        r = _run(["rsync", "-a", "--delete", f"{source}/", f"{target}/"])
        return {"status": "ok" if r["returncode"] == 0 else "error", "source": source, "target": target}

    # ── Token check ──
    elif name == "ee_token_check":
        token = args.get("token") or GITHUB_TOKEN
        if not token:
            return {"status": "no_token", "error": "No GitHub token provided"}

        r = _run(["git", "ls-remote", f"https://oauth2:{token}@github.com/{EE_REPO}.git", "HEAD"], timeout=30)
        if r["returncode"] == 0:
            commit = r["stdout"].split()[0] if r["stdout"] else "unknown"
            return {"status": "valid", "access": True, "head_commit": commit[:12]}
        return {"status": "invalid", "access": False, "error": r["stderr"]}

    # ── License status ──
    elif name == "ee_license_status":
        url = args["url"]
        db = args["db"]
        username = args.get("username", "admin")
        api_key = args["api_key"]

        try:
            import ssl
            ctx = ssl._create_unverified_context()
            common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True, context=ctx)
            uid = common.authenticate(db, username, api_key, {})
            if not uid:
                return {"error": "Authentication failed"}

            obj = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True, context=ctx)
            params = obj.execute_kw(db, uid, api_key, "ir.config_parameter", "search_read", [[
                ["key", "in", [
                    "database.expiration_date",
                    "database.expiration_reason",
                    "database.enterprise_code",
                ]]
            ]], {"fields": ["key", "value"]})

            license_info = {p["key"]: p["value"] for p in params}
            return {
                "status": "ok",
                "expiration_date": license_info.get("database.expiration_date", "unknown"),
                "expiration_reason": license_info.get("database.expiration_reason", "unknown"),
                "enterprise_code": license_info.get("database.enterprise_code", "not set"),
            }
        except Exception as e:
            return {"error": str(e)}

    # ── OCA conflicts ──
    elif name == "ee_oca_conflicts":
        ee_repo = _find_ee_repo(ee_dir)
        oca_dir = _resolve_oca_dir(args)

        ee_modules = {m["name"]: m for m in _scan_modules(ee_repo)}
        ee_names = set(ee_modules.keys())

        # Scan all OCA repos for modules
        oca_modules = {}
        if os.path.isdir(oca_dir):
            for repo_entry in os.listdir(oca_dir):
                repo_path = os.path.join(oca_dir, repo_entry)
                if not os.path.isdir(os.path.join(repo_path, ".git")):
                    continue
                for m in _scan_modules(repo_path):
                    m["oca_repo"] = repo_entry
                    oca_modules[m["name"]] = m

        oca_names = set(oca_modules.keys())

        # 1. Name collisions
        name_collisions = ee_names & oca_names

        # 2. Model overlaps for colliding modules
        conflicts = []
        for mod_name in sorted(name_collisions):
            ee_path = os.path.join(ee_repo, mod_name)
            oca_repo = oca_modules[mod_name].get("oca_repo", "")
            oca_path = os.path.join(oca_dir, oca_repo, mod_name)

            ee_inherits = _parse_inherits(ee_path)
            oca_inherits = _parse_inherits(oca_path)
            model_overlap = ee_inherits & oca_inherits

            conflicts.append({
                "module": mod_name,
                "ee_license": ee_modules[mod_name].get("license", ""),
                "oca_repo": oca_repo,
                "oca_license": oca_modules[mod_name].get("license", ""),
                "model_overlaps": sorted(model_overlap),
                "severity": "high" if model_overlap else "medium",
            })

        return {
            "total_ee": len(ee_names),
            "total_oca": len(oca_names),
            "name_collisions": len(name_collisions),
            "conflicts": conflicts,
        }

    # ── OCA recommend ──
    elif name == "ee_oca_recommend":
        module_name = args["module"]
        ee_repo = _find_ee_repo(ee_dir)
        oca_dir = _resolve_oca_dir(args)

        ee_modules = {m["name"]: m for m in _scan_modules(ee_repo)}
        ee_mod = ee_modules.get(module_name)

        # Find in OCA
        oca_mod = None
        oca_repo_name = ""
        if os.path.isdir(oca_dir):
            for repo_entry in os.listdir(oca_dir):
                repo_path = os.path.join(oca_dir, repo_entry)
                for m in _scan_modules(repo_path):
                    if m["name"] == module_name:
                        oca_mod = m
                        oca_repo_name = repo_entry
                        break
                if oca_mod:
                    break

        result = {"module": module_name}

        if ee_mod and oca_mod:
            result["exists_in"] = "both"
            result["ee"] = {
                "license": ee_mod.get("license", ""),
                "category": ee_mod.get("category", ""),
                "depends_count": len(ee_mod.get("depends", [])),
                "summary": ee_mod.get("summary", ""),
            }
            result["oca"] = {
                "repo": oca_repo_name,
                "license": oca_mod.get("license", ""),
                "category": oca_mod.get("category", ""),
                "depends_count": len(oca_mod.get("depends", [])),
                "summary": oca_mod.get("summary", ""),
            }

            # Recommendation logic
            if ee_mod.get("license") == "OEEL-1":
                result["recommendation"] = "ee"
                result["reason"] = (
                    "EE version is maintained by Odoo SA with professional support. "
                    "Requires Enterprise subscription. Choose OCA if no EE license."
                )
            else:
                result["recommendation"] = "evaluate"
                result["reason"] = "Both versions exist. Compare features and maintenance activity."

            # Check model overlaps
            ee_path = os.path.join(ee_repo, module_name)
            oca_path = os.path.join(oca_dir, oca_repo_name, module_name)
            model_overlap = _parse_inherits(ee_path) & _parse_inherits(oca_path)
            if model_overlap:
                result["warning"] = f"INCOMPATIBLE: both modify models {sorted(model_overlap)}. Cannot install both."

        elif ee_mod:
            result["exists_in"] = "ee_only"
            result["recommendation"] = "ee"
            result["reason"] = "No OCA alternative exists."
        elif oca_mod:
            result["exists_in"] = "oca_only"
            result["recommendation"] = "oca"
            result["reason"] = "No EE version exists."
        else:
            result["exists_in"] = "neither"
            result["error"] = "Module not found in EE or OCA."

        return result

    return {"error": f"Unknown tool: {name}"}


# ─── Main ──────────────────────────────────────────────────

async def main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
