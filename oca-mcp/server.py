"""
OCA MCP Server — OCA Addon Repository Management

Manages OCA (Odoo Community Association) addon repositories with two modes:
- Direct: git operations in /opt/odoo/{version}/oca/ (self-hosted)
- Buffered: git operations in /repos/{instance}/oca/ (odoo.sh workflow)

Wraps OCA maintainer-tools CLI commands + custom git/search operations.
"""
import ast
import glob
import json
import logging
import os
import subprocess
import sys
from typing import Any, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("oca-mcp")

BRANCH = os.environ.get("ODOO_BRANCH", "19.0")
DEFAULT_DIRECT_DIR = os.environ.get("OCA_DIRECT_DIR", "/opt/odoo")
DEFAULT_BUFFER_DIR = os.environ.get("OCA_BUFFER_DIR", "/repos")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

server = Server("oca-mcp")


# ─── Helpers ──────────────────────────────────────────────

def _resolve_dir(args: dict) -> str:
    """Resolve working directory based on mode."""
    mode = args.get("mode", "direct")
    instance = args.get("instance", "")
    branch = args.get("branch", BRANCH)

    if mode == "buffered" and instance:
        base = os.path.join(DEFAULT_BUFFER_DIR, instance, "oca")
    else:
        base = os.path.join(DEFAULT_DIRECT_DIR, f"odoo-{branch}", "oca")

    os.makedirs(base, exist_ok=True)
    return base


def _run(cmd: list[str], cwd: str = None, timeout: int = 300) -> dict:
    """Execute command and return result."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"Timeout after {timeout}s"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def _scan_modules(path: str) -> list[dict]:
    """Scan directory for Odoo modules with manifests."""
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
            })
        except Exception:
            continue
    return modules


def _scan_repos(path: str) -> list[dict]:
    """Scan directory for git repositories."""
    repos = []
    if not os.path.isdir(path):
        return repos

    for entry in sorted(os.listdir(path)):
        git_dir = os.path.join(path, entry, ".git")
        if not os.path.isdir(git_dir):
            continue

        repo_path = os.path.join(path, entry)
        info = {"name": entry, "path": repo_path}

        # Branch
        r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        info["branch"] = r["stdout"] if r["returncode"] == 0 else "unknown"

        # Status
        r = _run(["git", "status", "--porcelain"], cwd=repo_path)
        info["clean"] = r["returncode"] == 0 and len(r["stdout"]) == 0

        # Last commit
        r = _run(["git", "log", "-1", "--format=%h %s", "--"], cwd=repo_path)
        info["last_commit"] = r["stdout"] if r["returncode"] == 0 else ""

        # Module count
        info["modules"] = len(_scan_modules(repo_path))

        repos.append(info)
    return repos


# ─── Tool definitions ─────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="oca_clone_all",
            description=(
                "Clone all OCA repositories for a branch using oca-clone-everything. "
                "Mode: 'direct' → /opt/odoo, 'buffered' → /repos/{instance}/oca/"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "default": BRANCH, "description": "Odoo branch (e.g. 19.0)"},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": "", "description": "Instance name (buffered mode)"},
                },
            },
        ),
        Tool(
            name="oca_clone_repo",
            description="Clone a specific OCA repository by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "OCA repo name (e.g. 'account-financial-tools')"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="oca_update",
            description="Git pull all OCA repos in the working directory (recursive).",
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
            name="oca_status",
            description="Show git status of all OCA repos (branch, clean, behind, modules count).",
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
            name="oca_search",
            description="Search for a module across all cloned OCA repos by name or keyword.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Module name or keyword to search"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="oca_deploy",
            description="Deploy OCA repos from buffer to target (rsync). Only in buffered mode.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string", "description": "Instance name"},
                    "target": {"type": "string", "description": "Target path (e.g. /opt/odoo/odoo-19.0/oca/)"},
                    "branch": {"type": "string", "default": BRANCH},
                    "repos": {"type": "array", "items": {"type": "string"}, "description": "Specific repos (empty = all)"},
                },
                "required": ["instance", "target"],
            },
        ),
        Tool(
            name="oca_link",
            description="Create symlink for OCA addon module in the Odoo addons path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "module": {"type": "string", "description": "Module name"},
                    "target_dir": {"type": "string", "default": "", "description": "Symlink target dir (default: auto-detect from addons.conf)"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["module"],
            },
        ),
        Tool(
            name="oca_gen_readme",
            description="Generate README for an OCA addon using oca-gen-addon-readme.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name (e.g. 'server-tools')"},
                    "addon": {"type": "string", "description": "Addon directory name"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["repo", "addon"],
            },
        ),
        Tool(
            name="oca_gen_table",
            description="Generate addons table in repo README.md using oca-gen-addons-table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="oca_gen_icon",
            description="Generate default OCA icon for addon(s) using oca-gen-addon-icon.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name"},
                    "addon": {"type": "string", "default": "", "description": "Specific addon (empty = all in repo)"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="oca_gen_requirements",
            description="Generate requirements.txt from addon external_dependencies using oca-gen-external-dependencies.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["repo"],
            },
        ),
        Tool(
            name="oca_changelog",
            description="Generate CHANGELOG from newsfragments using oca-towncrier.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name"},
                    "addon": {"type": "string", "description": "Addon directory name"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["repo", "addon"],
            },
        ),
        Tool(
            name="oca_migrate",
            description="Migrate OCA repos to a new Odoo version branch using oca-migrate-branch.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_branch": {"type": "string", "description": "Source branch (e.g. 18.0)"},
                    "target_branch": {"type": "string", "description": "Target branch (e.g. 19.0)"},
                    "repos": {"type": "array", "items": {"type": "string"}, "description": "Specific repos to migrate"},
                },
                "required": ["source_branch", "target_branch"],
            },
        ),
        Tool(
            name="oca_fix_website",
            description="Fix website URL in addon manifests using oca-fix-manifest-website.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name"},
                    "url": {"type": "string", "description": "Website URL"},
                    "branch": {"type": "string", "default": BRANCH},
                    "mode": {"type": "string", "enum": ["direct", "buffered"], "default": "direct"},
                    "instance": {"type": "string", "default": ""},
                },
                "required": ["repo", "url"],
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
    oca_dir = _resolve_dir(args)
    branch = args.get("branch", BRANCH)

    if name == "oca_clone_all":
        r = _run(
            ["oca-clone-everything", "--target-branch", branch],
            cwd=oca_dir, timeout=1800,
        )
        return {
            "status": "ok" if r["returncode"] == 0 else "error",
            "directory": oca_dir,
            "branch": branch,
            "output": r["stdout"][-2000:] if r["stdout"] else r["stderr"][-2000:],
        }

    elif name == "oca_clone_repo":
        repo = args["repo"]
        url = f"https://github.com/OCA/{repo}.git"
        if GITHUB_TOKEN:
            url = f"https://oauth2:{GITHUB_TOKEN}@github.com/OCA/{repo}.git"
        repo_path = os.path.join(oca_dir, repo)
        if os.path.isdir(repo_path):
            r = _run(["git", "pull"], cwd=repo_path)
            action = "updated"
        else:
            r = _run(["git", "clone", "--branch", branch, url, repo_path], timeout=120)
            action = "cloned"
        return {
            "status": "ok" if r["returncode"] == 0 else "error",
            "action": action,
            "repo": repo,
            "path": repo_path,
            "output": r["stderr"] if r["returncode"] != 0 else "",
        }

    elif name == "oca_update":
        repos = _scan_repos(oca_dir)
        updated = []
        errors = []
        for repo in repos:
            r = _run(["git", "fetch", "--all"], cwd=repo["path"])
            r2 = _run(["git", "pull"], cwd=repo["path"])
            if r2["returncode"] == 0:
                updated.append(repo["name"])
            else:
                errors.append({"repo": repo["name"], "error": r2["stderr"]})
        return {
            "status": "ok",
            "directory": oca_dir,
            "updated": len(updated),
            "errors": errors,
        }

    elif name == "oca_status":
        repos = _scan_repos(oca_dir)
        return {
            "directory": oca_dir,
            "total_repos": len(repos),
            "repos": repos,
        }

    elif name == "oca_search":
        query = args["query"].lower()
        results = []
        for entry in sorted(os.listdir(oca_dir)) if os.path.isdir(oca_dir) else []:
            repo_path = os.path.join(oca_dir, entry)
            if not os.path.isdir(os.path.join(repo_path, ".git")):
                continue
            for module in _scan_modules(repo_path):
                if query in module["name"] or query in module.get("summary", "").lower():
                    module["repo"] = entry
                    results.append(module)
        return {
            "query": query,
            "found": len(results),
            "modules": results[:50],
        }

    elif name == "oca_deploy":
        instance = args["instance"]
        target = args["target"]
        source = os.path.join(DEFAULT_BUFFER_DIR, instance, "oca")
        repos_filter = args.get("repos", [])

        if not os.path.isdir(source):
            return {"error": f"Buffer directory not found: {source}"}

        deployed = []
        for entry in sorted(os.listdir(source)):
            if repos_filter and entry not in repos_filter:
                continue
            src = os.path.join(source, entry)
            if not os.path.isdir(os.path.join(src, ".git")):
                continue
            dst = os.path.join(target, entry)
            r = _run(["rsync", "-a", "--delete", f"{src}/", f"{dst}/"])
            if r["returncode"] == 0:
                deployed.append(entry)

        return {"status": "ok", "deployed": deployed, "target": target}

    elif name == "oca_link":
        module_name = args["module"]
        target_dir = args.get("target_dir") or f"/var/lib/odoo/.local/share/Odoo/addons/{branch}"

        # Find module in OCA repos
        source_path = None
        for entry in os.listdir(oca_dir) if os.path.isdir(oca_dir) else []:
            candidate = os.path.join(oca_dir, entry, module_name)
            if os.path.isfile(os.path.join(candidate, "__manifest__.py")):
                source_path = candidate
                break

        if not source_path:
            return {"error": f"Module '{module_name}' not found in OCA repos at {oca_dir}"}

        os.makedirs(target_dir, exist_ok=True)
        link_path = os.path.join(target_dir, module_name)

        if os.path.islink(link_path):
            current = os.readlink(link_path)
            if current == source_path:
                return {"status": "already_linked", "module": module_name, "path": link_path}
            os.unlink(link_path)

        os.symlink(source_path, link_path)
        return {"status": "linked", "module": module_name, "source": source_path, "link": link_path}

    # ── OCA maintainer-tools wrappers ──

    elif name == "oca_gen_readme":
        repo = args["repo"]
        addon = args["addon"]
        repo_path = os.path.join(oca_dir, repo)
        r = _run([
            "oca-gen-addon-readme",
            f"--repo-name={repo}", f"--branch={branch}",
            f"--addon-dir={addon}", "--no-commit",
        ], cwd=repo_path)
        return {"status": "ok" if r["returncode"] == 0 else "error", "output": r["stdout"] or r["stderr"]}

    elif name == "oca_gen_table":
        repo = args["repo"]
        repo_path = os.path.join(oca_dir, repo)
        r = _run(["oca-gen-addons-table", "--addons-dir=.", "--no-commit"], cwd=repo_path)
        return {"status": "ok" if r["returncode"] == 0 else "error", "output": r["stdout"] or r["stderr"]}

    elif name == "oca_gen_icon":
        repo = args["repo"]
        addon = args.get("addon", "")
        repo_path = os.path.join(oca_dir, repo)
        cmd = ["oca-gen-addon-icon", "--no-commit"]
        if addon:
            cmd.extend([f"--addon-dir={addon}"])
        else:
            cmd.extend(["--addons-dir=."])
        r = _run(cmd, cwd=repo_path)
        return {"status": "ok" if r["returncode"] == 0 else "error", "output": r["stdout"] or r["stderr"]}

    elif name == "oca_gen_requirements":
        repo = args["repo"]
        repo_path = os.path.join(oca_dir, repo)
        r = _run(["oca-gen-external-dependencies"], cwd=repo_path)
        return {"status": "ok" if r["returncode"] == 0 else "error", "output": r["stdout"] or r["stderr"]}

    elif name == "oca_changelog":
        repo = args["repo"]
        addon = args["addon"]
        repo_path = os.path.join(oca_dir, repo)
        r = _run([
            "oca-towncrier", f"--repo={repo}", f"--addon-dir={addon}", "--no-commit",
        ], cwd=repo_path)
        return {"status": "ok" if r["returncode"] == 0 else "error", "output": r["stdout"] or r["stderr"]}

    elif name == "oca_migrate":
        source = args["source_branch"]
        target = args["target_branch"]
        cmd = ["oca-migrate-branch", source, target]
        repos = args.get("repos", [])
        if repos:
            cmd.extend(["-p"] + repos)
        r = _run(cmd, timeout=600)
        return {"status": "ok" if r["returncode"] == 0 else "error", "output": r["stdout"] or r["stderr"]}

    elif name == "oca_fix_website":
        repo = args["repo"]
        url = args["url"]
        repo_path = os.path.join(oca_dir, repo)
        r = _run(["oca-fix-manifest-website", url, f"--addons-dir={repo_path}"])
        return {"status": "ok" if r["returncode"] == 0 else "error", "output": r["stdout"] or r["stderr"]}

    return {"error": f"Unknown tool: {name}"}


# ─── Main ──────────────────────────────────────────────────

async def main():
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
