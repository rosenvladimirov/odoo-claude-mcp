#!/usr/bin/env python3
"""
odoo_connect_cli.py — CLI connection manager for Odoo RPC MCP server.

Usage:
    python odoo_connect_cli.py list
    python odoo_connect_cli.py add <name> [--url URL --db DB --user USER --api-key KEY]
    python odoo_connect_cli.py edit <name>
    python odoo_connect_cli.py delete <name>
    python odoo_connect_cli.py test [name]
    python odoo_connect_cli.py import <file>
    python odoo_connect_cli.py export [file]
    python odoo_connect_cli.py ssh-add <name>
    python odoo_connect_cli.py ssh-remove <name>

Stores connections in the same JSON format as the GUI (odoo_connect.py).
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import xmlrpc.client
from pathlib import Path

# ── Config paths ──────────────────────────────────────────────────────

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / ".odoo_connections"
ENV_CONFIG = os.environ.get("ODOO_CONNECTIONS_FILE", "")
CONFIG_DIR = Path(ENV_CONFIG).parent if ENV_CONFIG else DEFAULT_CONFIG_DIR
CONFIG_FILE = Path(ENV_CONFIG) if ENV_CONFIG else DEFAULT_CONFIG_DIR / "connections.json"
SSH_CONFIG = Path.home() / ".ssh" / "config"

# ── Colors ────────────────────────────────────────────────────────────

NO_COLOR = os.environ.get("NO_COLOR", "")


def _c(code, text):
    if NO_COLOR or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t): return _c("32", t)
def red(t): return _c("31", t)
def yellow(t): return _c("33", t)
def cyan(t): return _c("36", t)
def bold(t): return _c("1", t)
def dim(t): return _c("2", t)


# ── Data helpers ──────────────────────────────────────────────────────


def load_connections() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def save_connections(connections: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(connections, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def test_connection(url: str, db: str, user: str, api_key: str) -> tuple[int, dict]:
    common = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/common", allow_none=True
    )
    uid = common.authenticate(db, user, api_key, {})
    if not uid:
        raise Exception("Authentication failed — check credentials.")
    version = common.version()
    return uid, version


# ── SSH helpers ───────────────────────────────────────────────────────


def _parse_ssh_hosts() -> set[str]:
    hosts = set()
    if not SSH_CONFIG.exists():
        return hosts
    for line in SSH_CONFIG.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*Host\s+(.+)", line, re.IGNORECASE)
        if m:
            for h in m.group(1).split():
                if "*" not in h and "?" not in h:
                    hosts.add(h)
    return hosts


def save_ssh_alias(alias, hostname, ssh_user, port=22, auth="agent", identity_file=None):
    if alias in _parse_ssh_hosts():
        return False, f"SSH alias '{alias}' already exists."
    SSH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    block = f"\nHost {alias}\n    HostName {hostname}\n    User {ssh_user}\n"
    if port != 22:
        block += f"    Port {port}\n"
    if auth == "agent":
        block += "    ForwardAgent yes\n"
    elif auth == "key" and identity_file:
        block += f"    IdentityFile {identity_file}\n    IdentitiesOnly yes\n"
    elif auth == "password":
        block += "    PasswordAuthentication yes\n    PubkeyAuthentication no\n"
    with open(SSH_CONFIG, "a", encoding="utf-8") as f:
        f.write(block)
    SSH_CONFIG.chmod(0o600)
    return True, f"SSH alias '{alias}' added."


def remove_ssh_alias(alias) -> bool:
    if not SSH_CONFIG.exists():
        return False
    lines = SSH_CONFIG.read_text(encoding="utf-8").splitlines(keepends=True)
    new, skip, removed = [], False, False
    for line in lines:
        if re.match(r"^\s*Host\s+" + re.escape(alias) + r"\s*$", line, re.IGNORECASE):
            skip, removed = True, True
            continue
        if skip and re.match(r"^\s*Host\s+", line, re.IGNORECASE):
            skip = False
        if skip and not line.strip():
            skip = False
            continue
        if not skip:
            new.append(line)
    if removed:
        SSH_CONFIG.write_text("".join(new), encoding="utf-8")
        SSH_CONFIG.chmod(0o600)
    return removed


# ── Interactive prompt ────────────────────────────────────────────────


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    prompt_text = f"  {label}{suffix}: "
    if secret:
        value = getpass.getpass(prompt_text)
    else:
        value = input(prompt_text)
    return value.strip() or default


def _prompt_connection(existing: dict | None = None) -> dict:
    """Interactive prompts for connection fields."""
    e = existing or {}
    print()
    url = _prompt("URL", e.get("url", "http://localhost:8069"))
    db = _prompt("Database", e.get("db", ""))
    user = _prompt("User", e.get("user", "admin"))
    api_key = _prompt("API Key", e.get("api_key", ""), secret=True)
    protocol = _prompt("Protocol (xmlrpc/jsonrpc)", e.get("protocol", "xmlrpc"))

    conn = {"url": url.rstrip("/"), "db": db, "user": user, "api_key": api_key}
    if protocol != "xmlrpc":
        conn["protocol"] = protocol

    # SSH?
    ssh_existing = e.get("ssh", {})
    add_ssh = _prompt("Configure SSH? (y/n)", "y" if ssh_existing else "n")
    if add_ssh.lower() == "y":
        ssh_host = _prompt("  SSH Host", ssh_existing.get("host", ""))
        ssh_user = _prompt("  SSH User", ssh_existing.get("user", ""))
        ssh_port = _prompt("  SSH Port", str(ssh_existing.get("port", 22)))
        ssh_auth = _prompt("  SSH Auth (agent/key/password)", ssh_existing.get("auth", "agent"))
        ssh_keyfile = ""
        if ssh_auth == "key":
            ssh_keyfile = _prompt("  Key File", ssh_existing.get("identity_file", ""))
        conn["ssh"] = {
            "host": ssh_host,
            "user": ssh_user,
            "port": int(ssh_port),
            "auth": ssh_auth,
            "identity_file": ssh_keyfile,
        }

    return conn


# ── Commands ──────────────────────────────────────────────────────────


def cmd_list(args):
    connections = load_connections()
    if not connections:
        print(dim("  No connections configured."))
        print(dim(f"  Config: {CONFIG_FILE}"))
        return

    print(f"\n  {bold('Odoo Connections')} ({len(connections)})\n")
    for name, conn in connections.items():
        url = conn.get("url", "")
        db = conn.get("db", "")
        user = conn.get("user", "")
        ssh = "  SSH" if conn.get("ssh") else ""
        print(f"  {cyan(name):30s}  {url}")
        print(f"  {'':30s}  {dim(f'db={db}  user={user}')}{yellow(ssh)}")
    print(f"\n  {dim(f'Config: {CONFIG_FILE}')}")


def cmd_add(args):
    connections = load_connections()
    name = args.name

    if name in connections and not args.force:
        print(red(f"  Connection '{name}' already exists. Use --force to overwrite."))
        return 1

    # Non-interactive if all required args provided
    if args.url and args.db and args.user and args.api_key:
        conn = {
            "url": args.url.rstrip("/"),
            "db": args.db,
            "user": args.user,
            "api_key": args.api_key,
        }
        if args.protocol and args.protocol != "xmlrpc":
            conn["protocol"] = args.protocol
    else:
        existing = connections.get(name, {})
        # Pre-fill from args
        if args.url:
            existing["url"] = args.url
        if args.db:
            existing["db"] = args.db
        if args.user:
            existing["user"] = args.user
        conn = _prompt_connection(existing)

    connections[name] = conn
    save_connections(connections)
    print(green(f"  ✔ Saved: {name}"))

    # Auto-test?
    if args.test:
        _do_test(name, conn)


def cmd_edit(args):
    connections = load_connections()
    name = args.name
    if name not in connections:
        print(red(f"  Connection '{name}' not found."))
        return 1

    print(f"  Editing {cyan(name)} — press Enter to keep current value")
    conn = _prompt_connection(connections[name])
    connections[name] = conn
    save_connections(connections)
    print(green(f"  ✔ Updated: {name}"))


def cmd_delete(args):
    connections = load_connections()
    name = args.name
    if name not in connections:
        print(red(f"  Connection '{name}' not found."))
        return 1

    if not args.yes:
        confirm = input(f"  Delete '{name}'? (y/n): ")
        if confirm.lower() != "y":
            print(dim("  Cancelled."))
            return

    if connections[name].get("ssh"):
        remove_ssh_alias(name)
    del connections[name]
    save_connections(connections)
    print(green(f"  ✔ Deleted: {name}"))


def cmd_test(args):
    connections = load_connections()
    names = [args.name] if args.name else list(connections.keys())
    if not names:
        print(dim("  No connections to test."))
        return

    for name in names:
        conn = connections.get(name)
        if not conn:
            print(red(f"  {name}: not found"))
            continue
        _do_test(name, conn)


def _do_test(name: str, conn: dict):
    url = conn.get("url", "")
    db = conn.get("db", "")
    user = conn.get("user", "")
    api_key = conn.get("api_key", "")
    if not all([url, db, user, api_key]):
        print(red(f"  {name}: incomplete (missing url/db/user/api_key)"))
        return
    try:
        uid, version = test_connection(url, db, user, api_key)
        sv = version.get("server_version", "?")
        print(green(f"  ✔ {name}: uid={uid}, Odoo {sv}"))
    except Exception as e:
        print(red(f"  ✘ {name}: {e}"))


def cmd_import(args):
    src = Path(args.file)
    if not src.exists():
        print(red(f"  File not found: {src}"))
        return 1

    imported = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(imported, dict):
        print(red("  Invalid format — expected JSON object {name: {url, db, ...}}"))
        return 1

    connections = load_connections()
    added, updated = 0, 0
    for name, conn in imported.items():
        if name in connections:
            updated += 1
        else:
            added += 1
        connections[name] = conn

    save_connections(connections)
    print(green(f"  ✔ Imported: {added} new, {updated} updated (total: {len(connections)})"))


def cmd_export(args):
    connections = load_connections()
    if args.file:
        dest = Path(args.file)
        dest.write_text(
            json.dumps(connections, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(green(f"  ✔ Exported {len(connections)} connections to {dest}"))
    else:
        print(json.dumps(connections, indent=2, ensure_ascii=False))


def cmd_ssh_add(args):
    connections = load_connections()
    name = args.name
    conn = connections.get(name)
    if not conn:
        print(red(f"  Connection '{name}' not found."))
        return 1
    ssh = conn.get("ssh")
    if not ssh:
        print(red(f"  Connection '{name}' has no SSH config."))
        return 1
    ok, msg = save_ssh_alias(
        name, ssh["host"], ssh["user"],
        port=ssh.get("port", 22),
        auth=ssh.get("auth", "agent"),
        identity_file=ssh.get("identity_file"),
    )
    print(green(f"  ✔ {msg}") if ok else yellow(f"  ⚠ {msg}"))


def cmd_ssh_remove(args):
    if remove_ssh_alias(args.name):
        print(green(f"  ✔ SSH alias '{args.name}' removed."))
    else:
        print(yellow(f"  SSH alias '{args.name}' not found."))


def cmd_ssh_test(args):
    connections = load_connections()
    conn = connections.get(args.name, {})
    ssh = conn.get("ssh", {})
    host = ssh.get("host") or args.name
    user = ssh.get("user", "root")
    port = ssh.get("port", 22)

    print(f"  Testing SSH: {user}@{host}:{port} ...")
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             "-p", str(port), f"{user}@{host}", "echo ok"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print(green(f"  ✔ SSH OK: {user}@{host}:{port}"))
        else:
            err = result.stderr.strip() or f"Exit code {result.returncode}"
            print(red(f"  ✘ {err}"))
    except subprocess.TimeoutExpired:
        print(red("  ✘ SSH connection timed out (5s)"))
    except Exception as e:
        print(red(f"  ✘ {e}"))


# ── CLI ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="odoo_connect_cli",
        description="Odoo connection manager (CLI)",
    )
    parser.add_argument(
        "--config", help="Path to connections.json",
    )
    sub = parser.add_subparsers(dest="command")

    # list
    sub.add_parser("list", aliases=["ls"], help="List all connections")

    # add
    p_add = sub.add_parser("add", help="Add a new connection")
    p_add.add_argument("name", help="Connection name (alias)")
    p_add.add_argument("--url", help="Odoo URL")
    p_add.add_argument("--db", help="Database name")
    p_add.add_argument("--user", help="Username")
    p_add.add_argument("--api-key", dest="api_key", help="API key")
    p_add.add_argument("--protocol", default="xmlrpc", help="xmlrpc or jsonrpc")
    p_add.add_argument("--force", "-f", action="store_true", help="Overwrite if exists")
    p_add.add_argument("--test", "-t", action="store_true", help="Test after saving")

    # edit
    p_edit = sub.add_parser("edit", help="Edit an existing connection")
    p_edit.add_argument("name", help="Connection name")

    # delete
    p_del = sub.add_parser("delete", aliases=["rm"], help="Delete a connection")
    p_del.add_argument("name", help="Connection name")
    p_del.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    # test
    p_test = sub.add_parser("test", help="Test connection(s)")
    p_test.add_argument("name", nargs="?", help="Connection name (all if omitted)")

    # import
    p_imp = sub.add_parser("import", help="Import connections from JSON file")
    p_imp.add_argument("file", help="JSON file path")

    # export
    p_exp = sub.add_parser("export", help="Export connections to JSON")
    p_exp.add_argument("file", nargs="?", help="Output file (stdout if omitted)")

    # ssh-add
    p_ssh_add = sub.add_parser("ssh-add", help="Add SSH alias from connection config")
    p_ssh_add.add_argument("name", help="Connection name")

    # ssh-remove
    p_ssh_rm = sub.add_parser("ssh-remove", help="Remove SSH alias")
    p_ssh_rm.add_argument("name", help="Alias name")

    # ssh-test
    p_ssh_test = sub.add_parser("ssh-test", help="Test SSH connection")
    p_ssh_test.add_argument("name", help="Connection name or host alias")

    args = parser.parse_args()

    # Override config path
    if args.config:
        global CONFIG_FILE, CONFIG_DIR
        CONFIG_FILE = Path(args.config)
        CONFIG_DIR = CONFIG_FILE.parent

    commands = {
        "list": cmd_list, "ls": cmd_list,
        "add": cmd_add,
        "edit": cmd_edit,
        "delete": cmd_delete, "rm": cmd_delete,
        "test": cmd_test,
        "import": cmd_import,
        "export": cmd_export,
        "ssh-add": cmd_ssh_add,
        "ssh-remove": cmd_ssh_remove,
        "ssh-test": cmd_ssh_test,
    }

    if args.command in commands:
        result = commands[args.command](args)
        sys.exit(result or 0)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
