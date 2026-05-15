"""MCP Supervisor plugin — Odoo addons-topology admin (v3.x, ADMIN class).

Owns the *one* rule: the canonical home of every Odoo module is the git
checkout bind-mounted into the odoo container as ``/opt/odoo`` (on the host
it is ``/opt/odoo/odoo-<ver>/{rv,oca,ee,...}/<repo>/<module>``).  The two
addons layers Odoo actually reads -- the data-dir farm
``/var/lib/odoo/.local/share/Odoo/addons/<ver>`` and ``/mnt/extra-addons``
-- live INSIDE the container's docker volumes and must contain **only
symlinks into /opt**, never real module copies.  Stale real-dir copies
there were the root cause of the 2026-05 shadowing mess.

This service is the programmatic, dry-run-safe replacement for the manual
SSH symlink archaeology.  It is container/volume-aware: it ``docker
inspect``s the odoo container on the target host to resolve the real host
paths of the bind (/opt/odoo) and the two farm volumes, then operates on
them as root.  Symlinks always point at the *in-container* ``/opt/odoo``
path (resolved by Odoo at runtime).

All tools are ADMIN (gateway/.mcp.json gating; bound to 127.0.0.1).

Tools
  supervisor_targets()                              SSH-capable hosts
  supervisor_status(target, container, version)     drift report
  supervisor_rebuild(target, container, version...) pure-symlink rebuild
  supervisor_run(target, version, ...)              orchestrate supervisor.py

Env
  SUPERVISOR_CONNECTIONS  connections.json path
                          (default ~/.claude/odoo_connections/connections.json)
  SUPERVISOR_SSH_OPTS     extra ssh options (space-separated)
"""
from __future__ import annotations

import json
import os
import re
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

mcp = FastMCP("supervisor")


# ─── connections / SSH ──────────────────────────────────────────

def _load_connections() -> dict[str, Any]:
    with open(CONNECTIONS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def _ssh_targets() -> dict[str, dict[str, Any]]:
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
    argv.append(f"{ssh.get('user', 'root')}@{ssh['host']}")
    return argv


def _run_ssh(target: str, remote_cmd: str, timeout: int = 120,
             stdin: Optional[str] = None) -> dict[str, Any]:
    targets = _ssh_targets()
    if target not in targets:
        return {"ok": False, "error": f"unknown target {target!r}; "
                f"known: {sorted(targets)}"}
    argv = _ssh_argv(targets[target]) + [remote_cmd]
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True,
                            text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ssh timeout after {timeout}s"}
    return {"ok": p.returncode == 0, "rc": p.returncode,
            "stdout": p.stdout, "stderr": p.stderr.strip()[-2000:]}


def _norm_series(version: str) -> str:
    """'18' -> '18.0' ; '18.0' stays. The Odoo series string."""
    v = str(version).strip()
    return f"{v}.0" if re.fullmatch(r"\d+", v) else v


# ─── remote probe (container/volume-aware; runs via `python3 -`) ─

# argv: <container> <series>
# docker-inspects the odoo container, resolves the host paths of the
# /opt/odoo bind + the two farm volumes, scans canonical modules and
# classifies every farm entry. Emits one JSON line.
_PROBE = r"""
import json, os, subprocess, sys
container, series = sys.argv[1], sys.argv[2]
IGNORE = {"setup", ".git", "__pycache__"}

def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()

try:
    mounts = json.loads(sh("docker", "inspect", "-f", "{{json .Mounts}}",
                           container) or "[]")
except Exception as e:
    print(json.dumps({"ok": False, "error": "inspect failed: %s" % e}))
    raise SystemExit(0)

dest = {}
for m in mounts:
    dest[m.get("Destination")] = m
host_source = (dest.get("/opt/odoo") or {}).get("Source")
mnt_src = (dest.get("/mnt/extra-addons") or {}).get("Source")
vlib = (dest.get("/var/lib/odoo") or {}).get("Source")
data_farm = os.path.join(vlib, ".local/share/Odoo/addons", series) if vlib else None
if not host_source:
    print(json.dumps({"ok": False,
        "error": "no /opt/odoo bind on container %s" % container,
        "destinations": sorted(d for d in dest if d)}))
    raise SystemExit(0)

def is_mod(d):
    return (os.path.isfile(os.path.join(d, "__manifest__.py"))
            or os.path.isfile(os.path.join(d, "__openerp__.py")))

canonical = {}                  # name -> host path
def walk(d):
    try:
        es = sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return
    for e in es:
        if not e.is_dir(follow_symlinks=False) or e.name in IGNORE \
                or e.name.startswith("."):
            continue
        if is_mod(e.path):
            canonical.setdefault(e.name, e.path)
        else:
            walk(e.path)
walk(host_source)

def ctgt(hp):                   # host /opt/odoo/odoo-x/rv/.. -> /opt/odoo/rv/..
    return os.path.join("/opt/odoo", os.path.relpath(hp, host_source))

layers = {}
for label, layer in (("data_farm", data_farm), ("mnt", mnt_src)):
    if not layer or not os.path.isdir(layer):
        layers[label] = {"__path__": layer, "__missing__": True}
        continue
    info = {"__path__": layer}
    for e in sorted(os.scandir(layer), key=lambda x: x.name):
        n = e.name
        if n in IGNORE or n.startswith("."):
            continue
        p = e.path
        if os.path.islink(p):
            # Symlinks point at the IN-CONTAINER /opt/odoo path; judge by
            # the target string, never host-side os.path.exists (that path
            # does not resolve on the host -> false "dangling").
            tg = os.readlink(p)
            if n in canonical:
                info[n] = {"type": "ok" if tg == ctgt(canonical[n])
                           else "symlink_other", "target": tg}
            else:
                info[n] = {"type": "symlink_orphan", "target": tg}
        elif e.is_dir(follow_symlinks=False) and is_mod(p):
            info[n] = {"type": "stale_realdir" if n in canonical
                       else "orphan_realdir"}
    for n in canonical:
        if n not in info:
            info[n] = {"type": "missing"}
    layers[label] = info

print(json.dumps({"ok": True, "container": container, "series": series,
    "host_source": host_source, "container_source": "/opt/odoo",
    "data_farm": data_farm, "mnt": mnt_src,
    "canonical_count": len(canonical),
    "canonical": {n: ctgt(p) for n, p in canonical.items()},
    "layers": layers}))
"""


def _probe(target: str, container: str, series: str,
           timeout: int = 120) -> dict[str, Any]:
    cmd = "python3 - " + shlex.quote(container) + " " + shlex.quote(series)
    res = _run_ssh(target, cmd, timeout=timeout, stdin=_PROBE)
    if not res["ok"]:
        return {"ok": False, "error": res.get("error") or res.get("stderr"),
                "raw": res}
    try:
        return json.loads(res["stdout"].strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return {"ok": False, "error": f"probe parse failed: {exc}",
                "stdout": res["stdout"][-1500:], "stderr": res["stderr"]}


# ─── tools (ADMIN) ──────────────────────────────────────────────

@mcp.tool()
def supervisor_targets() -> Any:
    """List SSH-capable hosts (connections.json entries with an ssh block)."""
    try:
        t = _ssh_targets()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc),
                "connections_file": CONNECTIONS_FILE}
    return {"ok": True, "connections_file": CONNECTIONS_FILE,
            "targets": {n: {"host": s.get("host"),
                            "user": s.get("user", "root"),
                            "port": s.get("port", 22),
                            "proxy_jump": s.get("proxy_jump")}
                        for n, s in t.items()}}


@mcp.tool()
def supervisor_status(target: str, container: str, version: str) -> Any:
    """Drift report for one odoo container's addons farm.

    ``container`` = the odoo container name on the host (e.g. ``odoo18``
    for dev-18, ``odoo`` for dev-19).  ``version`` may be ``18`` or
    ``18.0``.  Resolves real host paths via ``docker inspect`` then
    classifies each farm entry: ok / stale_realdir / orphan_realdir /
    symlink_other / dangling / missing.
    """
    rep = _probe(target, container, _norm_series(version))
    if not rep.get("ok"):
        return rep
    summary = {}
    for label, entries in rep["layers"].items():
        if entries.get("__missing__"):
            summary[label] = {"__missing__": True,
                              "path": entries.get("__path__")}
            continue
        counts: dict[str, int] = {}
        for k, v in entries.items():
            if k == "__path__":
                continue
            counts[v["type"]] = counts.get(v["type"], 0) + 1
        summary[label] = {"path": entries.get("__path__"), **counts}
    return {"ok": True, "target": target, "container": container,
            "series": rep["series"], "host_source": rep["host_source"],
            "canonical_count": rep["canonical_count"], "summary": summary,
            "detail": rep["layers"]}


@mcp.tool()
def supervisor_rebuild(target: str, container: str, version: str,
                       dry_run: bool = True,
                       remove_orphans: bool = False) -> Any:
    """Make every farm layer a PURE symlink farm from /opt.

    For each canonical module ensure ``<layer>/<mod>`` is a symlink to the
    in-container ``/opt/odoo/...`` path; replace stale real-dirs / wrong
    symlinks.  Orphans (no /opt counterpart) kept unless ``remove_orphans``.
    ``dry_run=True`` (default) returns the plan only; ``dry_run=False``
    executes on the host as the SSH user (root).
    """
    rep = _probe(target, container, _norm_series(version))
    if not rep.get("ok"):
        return rep
    canon = rep["canonical"]
    actions: list[dict[str, str]] = []
    for label, entries in rep["layers"].items():
        layer_path = entries.get("__path__")
        if entries.get("__missing__"):
            actions.append({"layer": label, "op": "skip_missing_layer",
                             "path": layer_path})
            continue
        for name, ctgt in canon.items():
            cur = entries.get(name, {})
            t = cur.get("type")
            dst = f"{layer_path}/{name}"
            if t == "ok":
                continue
            if t in ("stale_realdir", "symlink_other"):
                actions.append({"layer": label, "op": "relink", "path": dst,
                                 "to": ctgt, "was": t})
            else:  # missing / absent
                actions.append({"layer": label, "op": "link", "path": dst,
                                 "to": ctgt})
        for name, meta in entries.items():
            if name == "__path__":
                continue
            if meta.get("type") in ("orphan_realdir", "symlink_orphan"):
                actions.append({
                    "layer": label,
                    "op": "remove_orphan" if remove_orphans else "keep_orphan",
                    "path": f"{layer_path}/{name}"})

    if dry_run:
        return {"ok": True, "dry_run": True, "target": target,
                "container": container, "series": rep["series"],
                "canonical_count": len(canon),
                "action_count": len(actions), "actions": actions}

    sh = ["set -e"]
    for a in actions:
        p = shlex.quote(a["path"])
        if a["op"] in ("relink", "link"):
            sh.append(f"rm -rf {p} && ln -s {shlex.quote(a['to'])} {p}")
        elif a["op"] == "remove_orphan":
            sh.append(f"rm -rf {p}")
    res = _run_ssh(target, "bash -s", stdin="\n".join(sh), timeout=240)
    return {"ok": res["ok"], "dry_run": False, "target": target,
            "applied": len([a for a in actions
                            if a["op"] in ("relink", "link",
                                           "remove_orphan")]),
            "exec": res}


@mcp.tool()
def supervisor_run(target: str, version: str, supervisor_path: str = "",
                   addons_conf: str = "", github_update: bool = False,
                   force_update: bool = False, timeout: int = 600) -> Any:
    """Orchestrate the canonical host ``supervisor.py`` (github / OCA / EE /
    requirements / full farm).  Delegates to the real tool.
    """
    series = _norm_series(version)
    sp = supervisor_path or f"/opt/odoo/odoo-{series}/supervisor.py"
    ac = addons_conf or os.path.join(os.path.dirname(sp), "addons.conf")
    flags = []
    if github_update:
        flags.append("--github-update")
    if force_update:
        flags.append("--force-update")
    cmd = (f"test -f {shlex.quote(sp)} && python3 {shlex.quote(sp)} "
           f"{shlex.quote(ac)} {' '.join(flags)} -v 2>&1 | tail -60")
    res = _run_ssh(target, cmd, timeout=timeout)
    return {"ok": res["ok"], "target": target, "supervisor": sp,
            "addons_conf": ac, "flags": flags,
            "output": res.get("stdout", "")[-6000:],
            "error": res.get("stderr") or res.get("error")}


if __name__ == "__main__":
    mcp.run()
