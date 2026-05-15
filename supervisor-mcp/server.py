"""MCP Supervisor plugin — Odoo addons-topology admin (v3.x, ADMIN class).

Owns the *one* rule: the canonical home of every Odoo module is the git
checkout under ``/opt/odoo/odoo-<ver>/{rv,oca,ee,deltatech}/<repo>/<module>``
(bind-mounted into the container as ``/opt/odoo``).  The two addons layers
Odoo actually reads -- the data-dir farm
``~/.local/share/Odoo/addons/<ver>`` and ``/mnt/extra-addons`` -- must
contain **only symlinks into /opt**, never real module copies.  Stale
real-dir copies there were the root cause of the 2026-05 shadowing mess.

This service is the programmatic, dry-run-safe replacement for the manual
SSH symlink archaeology.  All tools are ADMIN (gateway enforces the
``MCP_ADMIN_TOKEN_<tenant>`` token; this service is wired admin-only).

Tools
  supervisor_targets()                         list SSH-capable hosts
  supervisor_status(target, version)           drift report git <-> farm
  supervisor_rebuild(target, version, ...)     pure-symlink rebuild (dry-run)
  supervisor_run(target, version, ...)         orchestrate canonical supervisor.py

Env
  SUPERVISOR_CONNECTIONS   connections.json path
                           (default ~/.claude/odoo_connections/connections.json)
  SUPERVISOR_SSH_OPTS      extra ssh options (space-separated)
  SUPERVISOR_DEFAULT_CONTAINER_SOURCE   in-container /opt root (default /opt/odoo)
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

CONNECTIONS_FILE = os.path.expanduser(
    os.environ.get(
        "SUPERVISOR_CONNECTIONS",
        "~/.claude/odoo_connections/connections.json",
    )
)
EXTRA_SSH_OPTS = shlex.split(os.environ.get("SUPERVISOR_SSH_OPTS", ""))
DEFAULT_CONTAINER_SOURCE = os.environ.get(
    "SUPERVISOR_DEFAULT_CONTAINER_SOURCE", "/opt/odoo"
)

# Dirs that are never modules even if they contain a manifest-like file.
IGNORE = {"setup", ".git", "__pycache__"}

mcp = FastMCP("supervisor")


# ─── connections / SSH ──────────────────────────────────────────

def _load_connections() -> dict[str, Any]:
    with open(CONNECTIONS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def _ssh_targets() -> dict[str, dict[str, Any]]:
    """Connections that carry an ``ssh`` block = candidate hosts."""
    conns = _load_connections()
    return {
        name: c["ssh"]
        for name, c in conns.items()
        if isinstance(c, dict) and isinstance(c.get("ssh"), dict)
        and c["ssh"].get("host")
    }


def _ssh_argv(ssh: dict[str, Any]) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=12",
    ]
    if ssh.get("port"):
        argv += ["-p", str(ssh["port"])]
    if ssh.get("identity_file"):
        argv += ["-i", os.path.expanduser(ssh["identity_file"])]
    if ssh.get("proxy_jump"):
        argv += ["-J", ssh["proxy_jump"]]
    argv += EXTRA_SSH_OPTS
    user = ssh.get("user", "root")
    argv.append(f"{user}@{ssh['host']}")
    return argv


def _run_ssh(target: str, remote_cmd: str, timeout: int = 120,
             stdin: Optional[str] = None) -> dict[str, Any]:
    targets = _ssh_targets()
    if target not in targets:
        return {"ok": False, "error": f"unknown target {target!r}; "
                f"known: {sorted(targets)}"}
    argv = _ssh_argv(targets[target]) + [remote_cmd]
    try:
        p = subprocess.run(
            argv, input=stdin, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ssh timeout after {timeout}s"}
    return {
        "ok": p.returncode == 0,
        "rc": p.returncode,
        "stdout": p.stdout,
        "stderr": p.stderr.strip()[-2000:],
    }


# ─── remote probe (runs on the odoo host via `python3 -`) ───────

# Emits JSON: canonical modules (recursive dir-with-manifest under
# host_source) + classification of each entry in every farm layer.
_PROBE = r"""
import json, os, sys
host_source, container_source = sys.argv[1], sys.argv[2]
layers = sys.argv[3:]
IGNORE = {"setup", ".git", "__pycache__"}

def is_module(d):
    return (os.path.isfile(os.path.join(d, "__manifest__.py"))
            or os.path.isfile(os.path.join(d, "__openerp__.py")))

canonical = {}            # module name -> host path
def walk(d):
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return
    for e in entries:
        if not e.is_dir(follow_symlinks=False):
            continue
        if e.name in IGNORE or e.name.startswith("."):
            continue
        if is_module(e.path):
            canonical.setdefault(e.name, e.path)   # first wins
        else:
            walk(e.path)

if os.path.isdir(host_source):
    walk(host_source)

def container_path(host_path):
    # host /opt/odoo/odoo-<ver>/rv/..  ->  container /opt/odoo/rv/..
    rel = os.path.relpath(host_path, host_source)
    return os.path.join(container_source, rel)

report = {"host_source": host_source, "container_source": container_source,
          "canonical_count": len(canonical), "layers": {}}
for layer in layers:
    info = {}
    if os.path.isdir(layer):
        for e in sorted(os.scandir(layer), key=lambda x: x.name):
            n = e.name
            if n in IGNORE or n.startswith("."):
                continue
            p = e.path
            if os.path.islink(p):
                tgt = os.readlink(p)
                if not os.path.exists(p):
                    info[n] = {"type": "dangling", "target": tgt}
                elif n in canonical and os.path.realpath(p) == os.path.realpath(
                        container_path(canonical[n])) or tgt == container_path(
                        canonical.get(n, "")):
                    info[n] = {"type": "ok", "target": tgt}
                else:
                    info[n] = {"type": "symlink_other", "target": tgt}
            elif e.is_dir(follow_symlinks=False) and is_module(p):
                info[n] = {"type": "stale_realdir" if n in canonical
                           else "orphan_realdir"}
            # non-module dirs / files in the layer are ignored
        # modules present canonically but absent from this layer
        for n in canonical:
            if n not in info:
                info[n] = {"type": "missing"}
    else:
        info = {"__layer_missing__": True}
    report["layers"][layer] = info
print(json.dumps(report))
"""


def _probe(target: str, host_source: str, container_source: str,
           layers: list[str], timeout: int = 90) -> dict[str, Any]:
    cmd = "python3 - " + " ".join(
        shlex.quote(a) for a in [host_source, container_source, *layers]
    )
    res = _run_ssh(target, cmd, timeout=timeout, stdin=_PROBE)
    if not res["ok"]:
        return {"ok": False, "error": res.get("error") or res.get("stderr"),
                "raw": res}
    try:
        return {"ok": True, **json.loads(res["stdout"].strip().splitlines()[-1])}
    except (ValueError, IndexError) as exc:
        return {"ok": False, "error": f"probe parse failed: {exc}",
                "stdout": res["stdout"][-1500:], "stderr": res["stderr"]}


def _layers(version: str, target_dir: Optional[str]) -> list[str]:
    td = target_dir or (
        f"/var/lib/odoo/.local/share/Odoo/addons/{version}"
    )
    return [td, "/mnt/extra-addons"]


# ─── tools (ADMIN) ──────────────────────────────────────────────

@mcp.tool()
def supervisor_targets() -> Any:
    """List SSH-capable hosts (connections.json entries with an ssh block)."""
    try:
        t = _ssh_targets()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc),
                "connections_file": CONNECTIONS_FILE}
    return {
        "ok": True,
        "connections_file": CONNECTIONS_FILE,
        "targets": {
            n: {"host": s.get("host"), "user": s.get("user", "root"),
                "port": s.get("port", 22),
                "proxy_jump": s.get("proxy_jump")}
            for n, s in t.items()
        },
    }


@mcp.tool()
def supervisor_status(target: str, version: str,
                      host_source: Optional[str] = None,
                      container_source: Optional[str] = None,
                      target_dir: Optional[str] = None) -> Any:
    """Drift report: canonical /opt modules vs the two addons farm layers.

    ``host_source`` default ``/opt/odoo/odoo-<version>`` (path on the host),
    ``container_source`` default ``/opt/odoo`` (what symlinks must point at,
    resolved inside the odoo container).  Reports per layer: ok /
    stale_realdir / orphan_realdir / symlink_other / dangling / missing.
    """
    hs = host_source or f"/opt/odoo/odoo-{version}"
    cs = container_source or DEFAULT_CONTAINER_SOURCE
    rep = _probe(target, hs, cs, _layers(version, target_dir))
    if not rep.get("ok"):
        return rep
    summary = {}
    for layer, entries in rep["layers"].items():
        if entries.get("__layer_missing__"):
            summary[layer] = {"__layer_missing__": True}
            continue
        counts: dict[str, int] = {}
        for v in entries.values():
            counts[v["type"]] = counts.get(v["type"], 0) + 1
        summary[layer] = counts
    return {
        "ok": True, "target": target, "version": version,
        "host_source": hs, "container_source": cs,
        "canonical_count": rep["canonical_count"],
        "summary": summary, "detail": rep["layers"],
    }


@mcp.tool()
def supervisor_rebuild(target: str, version: str, dry_run: bool = True,
                       remove_orphans: bool = False,
                       host_source: Optional[str] = None,
                       container_source: Optional[str] = None,
                       target_dir: Optional[str] = None) -> Any:
    """Make every farm layer a PURE symlink farm from /opt.

    For each canonical module: ensure ``<layer>/<mod>`` is a symlink to the
    in-container canonical path; replace stale real-dir copies.  Orphans
    (farm modules with no /opt counterpart) are kept unless
    ``remove_orphans``.  ``dry_run=True`` (default) returns the plan only;
    ``dry_run=False`` executes it on the host as the SSH user.
    """
    hs = host_source or f"/opt/odoo/odoo-{version}"
    cs = container_source or DEFAULT_CONTAINER_SOURCE
    layers = _layers(version, target_dir)
    rep = _probe(target, hs, cs, layers)
    if not rep.get("ok"):
        return rep

    # canonical name -> in-container target path
    probe2 = _run_ssh(
        target,
        "python3 - " + " ".join(shlex.quote(a) for a in [hs, cs]),
        stdin=(
            "import json,os,sys\n"
            "hs,cs=sys.argv[1],sys.argv[2]\n"
            "IGN={'setup','.git','__pycache__'}\n"
            "out={}\n"
            "def mod(d):\n"
            " return os.path.isfile(d+'/__manifest__.py') or "
            "os.path.isfile(d+'/__openerp__.py')\n"
            "def w(d):\n"
            " import os\n"
            " try: es=sorted(os.scandir(d),key=lambda e:e.name)\n"
            " except OSError: return\n"
            " for e in es:\n"
            "  if not e.is_dir(follow_symlinks=False) or e.name in IGN "
            "or e.name.startswith('.'): continue\n"
            "  if mod(e.path): out.setdefault(e.name, os.path.join(cs,"
            "os.path.relpath(e.path,hs)))\n"
            "  else: w(e.path)\n"
            "w(hs); print(json.dumps(out))\n"
        ),
    )
    try:
        canon_target = json.loads(probe2["stdout"].strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"canonical map failed: {exc}",
                "raw": probe2}

    actions: list[dict[str, str]] = []
    for layer, entries in rep["layers"].items():
        if entries.get("__layer_missing__"):
            actions.append({"layer": layer, "op": "mkdir", "path": layer})
        for name, ctgt in canon_target.items():
            cur = entries.get(name, {}) if isinstance(entries, dict) else {}
            t = cur.get("type")
            dst = f"{layer}/{name}"
            if t == "ok":
                continue
            if t in ("stale_realdir", "symlink_other", "dangling"):
                actions.append({"layer": layer, "op": "relink",
                                 "path": dst, "to": ctgt, "was": t})
            elif t == "missing" or not t:
                actions.append({"layer": layer, "op": "link",
                                 "path": dst, "to": ctgt})
        for name, meta in (entries.items() if isinstance(entries, dict)
                           else []):
            if name == "__layer_missing__":
                continue
            if meta.get("type") == "orphan_realdir":
                actions.append({
                    "layer": layer,
                    "op": "remove_orphan" if remove_orphans else "keep_orphan",
                    "path": f"{layer}/{name}",
                })

    if dry_run:
        return {"ok": True, "dry_run": True, "target": target,
                "version": version, "canonical_count": len(canon_target),
                "action_count": len(actions), "actions": actions}

    # execute
    sh = ["set -e"]
    for a in actions:
        p = shlex.quote(a["path"])
        if a["op"] == "mkdir":
            sh.append(f"mkdir -p {p}")
        elif a["op"] in ("relink", "link"):
            sh.append(f"rm -rf {p} && ln -s {shlex.quote(a['to'])} {p}")
        elif a["op"] == "remove_orphan":
            sh.append(f"rm -rf {p}")
    res = _run_ssh(target, "bash -s", stdin="\n".join(sh), timeout=180)
    return {"ok": res["ok"], "dry_run": False, "target": target,
            "applied": len([a for a in actions
                            if a["op"] not in ("keep_orphan",)]),
            "exec": res}


@mcp.tool()
def supervisor_run(target: str, version: str, supervisor_path: str = "",
                   addons_conf: str = "", github_update: bool = False,
                   force_update: bool = False, timeout: int = 600) -> Any:
    """Orchestrate the canonical docker ``supervisor.py`` on the host
    (github sync / OCA / EE / requirements / full farm).  Delegates to the
    real tool instead of re-implementing it.

    ``supervisor_path`` default ``/opt/odoo/odoo-<version>/supervisor.py``;
    ``addons_conf`` default ``<dir>/addons.conf`` next to it.
    """
    sp = supervisor_path or f"/opt/odoo/odoo-{version}/supervisor.py"
    ac = addons_conf or os.path.join(os.path.dirname(sp), "addons.conf")
    flags = []
    if github_update:
        flags.append("--github-update")
    if force_update:
        flags.append("--force-update")
    cmd = (
        f"test -f {shlex.quote(sp)} && python3 {shlex.quote(sp)} "
        f"{shlex.quote(ac)} {' '.join(flags)} -v 2>&1 | tail -60"
    )
    res = _run_ssh(target, cmd, timeout=timeout)
    return {"ok": res["ok"], "target": target, "supervisor": sp,
            "addons_conf": ac, "flags": flags,
            "output": res.get("stdout", "")[-6000:],
            "error": res.get("stderr") or res.get("error")}


if __name__ == "__main__":
    mcp.run()
