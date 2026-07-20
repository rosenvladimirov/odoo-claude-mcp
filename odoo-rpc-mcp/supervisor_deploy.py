"""
v3 Supervisor remote control — дистанционно управление на supervisor.py
(Odoo addons orchestrator) през **Portainer Docker API**.

Защо Portainer, а не SSH: операторът е „затворен в Portainer" — няма изходящ
SSH до стаковете, но Portainer API е достъпен и вече валидиран
(docker-proxy exec/run работи). Виж ADR
`specs/mcp-supervisor-remote/adr/0001-transport-via-portainer-exec.md`.

Два транспорта (`supervisor.transport`: auto | sidecar | oneshot):

  * **sidecar** (препоръчан) — `exec` в ВЕЧЕ работещ toolbox контейнер. Нужен е
    зад Cloudflare tunnel, където bodyless `POST /containers/{id}/start` се
    пре-chunk-ва и Docker >= 29 го отхвърля с 400 (виж ADR-0002). Sidecar-ът се
    стартира ЕДНОКРАТНО ръчно — `supervisor_sidecar_status` дава командата.
  * **oneshot** (наследен) — вдигаме еднократен контейнер от slim toolbox образа
    (`supervisor-19.0-slim`), пускаме `supervisor <conf> <flags>`, събираме
    логовете и трием контейнера. Работи само на стандартно изложен Docker.

При `auto` наличието на `supervisor.sidecar` избира sidecar пътя.

БЕЗОПАСНОСТ:
  * `supervisor_status` = напълно read-only (`--github-status` → supervisor.py
    връща след git скана, БЕЗ symlinks/pip/chown; виж supervisor.py:1079).
  * Разрушителните режими (oca/ee/force/init) са DRY-RUN по подразбиране
    (MCP_SUPERVISOR_DRY_RUN=1): само план, никакъв контейнер. Реален run иска
    dry_run=false. ⚠️ В кодовата база НЯМА per-action TOTP примитив — TOTP е само
    втори фактор при identify; затова предпазната мярка тук е DRY-RUN + admin gate
    + `read_only` флаг на alias-а (както module_deploy).
  * Admin-principal gated в server.py (същата летва като module_deploy/fleet/secrets).

Config за целевия стак живее в connections.json под alias-а:
  "portainer": {"url": ..., "token": ..., "read_only": false, "endpoint_id": 1},
  "supervisor": {
      "image": "vladimirovrosen/odoo:supervisor-19.0-slim",   # по-добре digest-pin
      "conf_path": "/etc/odoo/addons.conf",
      "binds": ["<source_vol>:/opt/odoo", "<host_conf>:/etc/odoo/addons.conf"],
      "network": "bridge",           # трябва egress за --github-status
      "endpoint_id": 1
  }
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

try:
    import httpx
except Exception:  # httpx липсва → tool-овете връщат ясна грешка
    httpx = None

logger = logging.getLogger("supervisor_deploy")

SUPERVISOR_OPS_FILE = Path(os.environ.get(
    "SUPERVISOR_DEPLOY_OPS_FILE", "/data/supervisor_deploy_ops.jsonl"))
DRY_RUN = os.environ.get("MCP_SUPERVISOR_DRY_RUN", "1") == "1"
RUN_TIMEOUT = int(os.environ.get("MCP_SUPERVISOR_TIMEOUT", "1800"))
HTTP_TIMEOUT = float(os.environ.get("MCP_SUPERVISOR_HTTP_TIMEOUT", "60"))
DEFAULT_IMAGE = os.environ.get(
    "MCP_SUPERVISOR_IMAGE", "vladimirovrosen/odoo:supervisor-19.0-slim")
DEFAULT_CONF = "/etc/odoo/addons.conf"
DEFAULT_ENDPOINT_ID = int(os.environ.get("MCP_PORTAINER_ENDPOINT_ID", "1"))
# connections.json кандидати (огледало на module_deploy._CONN_CANDIDATES).
_CONN_CANDIDATES = [
    Path(os.environ.get("CONNECTIONS_FILE", "/data/connections.json")),
    Path("/config/connections.json"),
    Path.home() / "Проекти" / "odoo" / "odoo-18.0" / "claude.ai"
        / ".odoo_connections" / "connections.json",
]
# Деплойнатият gateway пази конекции per-user, НЕ в глобален connections.json.
_USERS_DIR = os.environ.get("MCP_SUPERVISOR_USERS_DIR", "/data/users")

# fail-closed: режим → фиксирани флагове. Непознат режим → reject (виж _resolve_mode).
_MODE_FLAGS: dict[str, list[str]] = {
    "status": ["--github-status"],          # read-only (git скан, без промени)
    "github_update": ["--github-update"],   # ъпдейтва git repos (no system changes)
    "github_sync": ["--github-only"],       # status + update на repos
    "oca": ["--addons-oca"],                # инсталира OCA addons (пипа системата)
    "ee": ["--addons-ee"],                  # инсталира EE addons
    "force": ["--force-update"],            # форс config/permissions
    "init": ["--init-container"],           # strict init (force + requirements)
}
# Режими, които РЕАЛНО мутират стака (symlinks/pip/chown) → DRY-RUN по подразбиране.
_DESTRUCTIVE_MODES = {"oca", "ee", "force", "init"}
# Режими без промени по стака (позволени и при read_only alias).
_READONLY_MODES = {"status"}

# Callbacks wired from server.py (за симетрия с module_deploy; тук ползваме само
# Portainer, но приемаме същия wire() интерфейс).
_ssh_execute: Callable | None = None
_ensure_ssh_master: Callable | None = None
_get_conn: Callable | None = None


def wire(*, ssh_execute: Callable | None = None,
         ensure_ssh_master: Callable | None = None,
         get_conn: Callable | None = None) -> None:
    """Inject server.py transport + RPC. Call once at startup."""
    global _ssh_execute, _ensure_ssh_master, _get_conn
    _ssh_execute = ssh_execute
    _ensure_ssh_master = ensure_ssh_master
    _get_conn = get_conn


# ─── Op ledger (огледало на module_deploy/fleet_manager) ───────────────────

def _ops_replay() -> list[dict]:
    out: list[dict] = []
    if not SUPERVISOR_OPS_FILE.is_file():
        return out
    try:
        with open(SUPERVISOR_OPS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return out


def _ops_append(record: dict) -> None:
    SUPERVISOR_OPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUPERVISOR_OPS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(SUPERVISOR_OPS_FILE, 0o600)
    except OSError:
        pass


# ─── connections.json reader ───────────────────────────────────────────────

def _read_conns(path) -> dict | None:
    """Чете connections.json; поддържа и flat {alias:{}} и {connections:{alias:{}}}."""
    try:
        conns = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(conns, dict):
        inner = conns.get("connections")
        return inner if isinstance(inner, dict) else conns
    return None


def _load_entry(alias: str) -> dict:
    # 1) статични глобални кандидати (module_deploy стил)
    for p in _CONN_CANDIDATES:
        try:
            if p.exists():
                root = _read_conns(p)
                if root and alias in root:
                    return root[alias]
        except Exception:
            continue
    # 2) per-user store на деплойнатия gateway (/data/users/<user>/connections.json).
    #    Tool-ът е admin-gated → сканирането на всички потребители е приемливо.
    try:
        for f in sorted(glob.glob(os.path.join(_USERS_DIR, "*", "connections.json"))):
            root = _read_conns(f)
            if root and alias in root:
                return root[alias]
    except Exception:
        pass
    return {}


def _get_supervisor_config(alias: str) -> dict:
    """Изтегля portainer + supervisor блоковете за alias-а от connections.json."""
    entry = _load_entry(alias)
    return {
        "portainer": dict(entry.get("portainer", {}) or {}),
        "supervisor": dict(entry.get("supervisor", {}) or {}),
        "ssh": entry.get("ssh", {}) or {},
        "found": bool(entry),
    }


# ─── Portainer Docker API клиент (per-alias url+token; няма готов в сървъра) ──

def _portainer_base(portainer: dict) -> tuple[str, dict]:
    url = (portainer.get("url") or "").rstrip("/")
    token = portainer.get("token") or ""
    headers = {"X-API-Key": token, "Content-Type": "application/json"}
    return url, headers


def _px(client, method: str, url: str, headers: dict, **kw):
    """Единичен Portainer HTTP call; вдига при не-2xx."""
    # Docker API >= v1.24 отхвърля тяло на /start; форсирай ПРАЗНО тяло на
    # bodyless POST-ове (иначе httpx праща chunked → "non-empty request body").
    if method == "POST" and "json" not in kw and "content" not in kw:
        kw["content"] = b""
    r = client.request(method, url, headers=headers, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"portainer {method} {url} -> HTTP {r.status_code}: "
                           f"{r.text[:300]}")
    return r


def _demux(raw: bytes) -> str:
    """Демултиплексира Docker attach потока (Tty=false → 8-байтов header
    [stream_type, 0,0,0, big-endian size]). Ако рамките не се разпознаят,
    връща суровия текст (контейнерът е бил с Tty=true)."""
    out: list[str] = []
    i, n = 0, len(raw)
    while i + 8 <= n:
        if raw[i] not in (0, 1, 2):          # не е валиден stream type → не е рамкиран
            return raw.decode("utf-8", "replace")
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        i += 8
        out.append(raw[i:i + size].decode("utf-8", "replace"))
        i += size
    if i != n:                               # остатък → потокът не е бил рамкиран
        return raw.decode("utf-8", "replace")
    return "".join(out)


def _inspect_container(client, dbase: str, headers: dict, name: str) -> dict:
    """Връща {found, running, id, image} за контейнер по име или id."""
    try:
        r = _px(client, "GET", f"{dbase}/containers/{name}/json", headers)
    except Exception as e:
        if "HTTP 404" in str(e):
            return {"found": False, "running": False}
        raise
    data = r.json()
    return {"found": True,
            "running": bool((data.get("State") or {}).get("Running")),
            "id": data.get("Id"),
            "image": (data.get("Config") or {}).get("Image")}


def _run_sidecar(portainer: dict, eid: int, container: str,
                 cmd: list[str]) -> dict:
    """Изпълнява cmd чрез `exec` в ВЕЧЕ работещ sidecar контейнер.

    Защо: зад Cloudflare tunnel bodyless `POST /containers/{id}/start` се
    пре-chunk-ва → Docker >= 29 го чете като непразно тяло и връща 400
    (ADR-0002). Exec заявките носят JSON тяло и минават чисто.
    Sidecar-ът се стартира ЕДНОКРАТНО извън MCP (виж supervisor_sidecar_status)."""
    if httpx is None:
        return {"error": "dependency_missing", "detail": "httpx not installed"}
    base, headers = _portainer_base(portainer)
    if not base or not headers.get("X-API-Key"):
        return {"error": "config", "detail": "portainer url/token missing for alias"}

    dbase = f"{base}/api/endpoints/{eid}/docker"
    with httpx.Client(timeout=HTTP_TIMEOUT, verify=False) as client:
        info = _inspect_container(client, dbase, headers, container)
        if not info["found"]:
            return {"error": "sidecar_missing", "sidecar": container,
                    "detail": f"container '{container}' not found on endpoint {eid}"}
        if not info["running"]:
            return {"error": "sidecar_not_running", "sidecar": container,
                    "detail": f"container '{container}' exists but is not running"}
        # 1) exec create (JSON тяло → минава през Cloudflare)
        r = _px(client, "POST", f"{dbase}/containers/{container}/exec", headers,
                json={"AttachStdout": True, "AttachStderr": True,
                      "Tty": False, "Cmd": cmd})
        exec_id = r.json().get("Id")
        # 2) exec start — блокира до край на командата
        sr = _px(client, "POST", f"{dbase}/exec/{exec_id}/start", headers,
                 json={"Detach": False, "Tty": False}, timeout=RUN_TIMEOUT)
        logs = _demux(sr.content)
        # 3) exit code
        ir = _px(client, "GET", f"{dbase}/exec/{exec_id}/json", headers)
        exit_code = ir.json().get("ExitCode")
    return {"exit_code": -1 if exit_code is None else exit_code,
            "logs": logs, "container": container, "transport": "sidecar"}


def _run_oneshot(portainer: dict, eid: int, image: str, cmd: list[str],
                 binds: list[str], network: str | None) -> dict:
    """Пуска еднократен контейнер през Portainer Docker proxy и връща
    {exit_code, logs, container}. Винаги трие контейнера накрая."""
    if httpx is None:
        return {"error": "dependency_missing", "detail": "httpx not installed"}
    base, headers = _portainer_base(portainer)
    if not base or not headers.get("X-API-Key"):
        return {"error": "config", "detail": "portainer url/token missing for alias"}

    dbase = f"{base}/api/endpoints/{eid}/docker"
    name = f"mcp-supervisor-{int(time.time())}"
    # LogConfig явно на json-file: при хост с друг драйвер (journald/local)
    # `GET /containers/{id}/logs` връща 0 байта и run-ът изглежда „тих".
    host_cfg = {"Binds": binds or [], "AutoRemove": False,
                "LogConfig": {"Type": "json-file", "Config": {}}}
    if network:
        host_cfg["NetworkMode"] = network
    create_body = {
        "Image": image,
        "Cmd": cmd,
        "Tty": True,               # Tty=true → логовете идват като чист текст (без 8-байт framing)
        "HostConfig": host_cfg,
        "Labels": {"mcp.supervisor": "1", "mcp.role": "supervisor-oneshot"},
    }
    cid = None
    with httpx.Client(timeout=HTTP_TIMEOUT, verify=False) as client:
        # 1) pull образа (repo:tag или repo@sha256)
        if "@" in image:
            repo, _, digest = image.partition("@")
            pull_q = {"fromImage": repo, "tag": digest}
        else:
            repo, _, tag = image.partition(":")
            pull_q = {"fromImage": repo, "tag": tag or "latest"}
        try:
            _px(client, "POST", f"{dbase}/images/create", headers, params=pull_q)
        except Exception as e:
            logger.warning("image pull warning (продължавам, може да е локален): %s", e)
        # 2) create
        r = _px(client, "POST", f"{dbase}/containers/create", headers,
                params={"name": name}, json=create_body)
        cid = r.json().get("Id")
        try:
            # 3) start
            _px(client, "POST", f"{dbase}/containers/{cid}/start", headers)
            # 4) wait (блокира до изход; RUN_TIMEOUT на HTTP нивото)
            wr = _px(client, "POST", f"{dbase}/containers/{cid}/wait", headers,
                     timeout=RUN_TIMEOUT)
            exit_code = wr.json().get("StatusCode", -1)
            # 5) logs (Tty=true → чист текст)
            lr = _px(client, "GET", f"{dbase}/containers/{cid}/logs", headers,
                     params={"stdout": 1, "stderr": 1, "timestamps": 0})
            logs = lr.text
        finally:
            # 6) cleanup — винаги
            if cid:
                try:
                    _px(client, "DELETE", f"{dbase}/containers/{cid}", headers,
                        params={"force": 1})
                except Exception as e:
                    logger.warning("container cleanup failed (%s): %s", cid, e)
    return {"exit_code": exit_code, "logs": logs, "container": name,
            "transport": "oneshot"}


# ─── Parse на supervisor --github-status изхода ────────────────────────────

def _parse_status(logs: str) -> dict:
    """Извлича обобщението от github_scan_and_report_repositories логовете."""
    def _num(label: str) -> int | None:
        m = re.search(rf"{label}:\s*(\d+)", logs)
        return int(m.group(1)) if m else None
    outdated = re.findall(r"-\s+(\S+):\s+(\d+)\s+commits behind", logs)
    return {
        "total": _num("Total repositories"),
        "outdated": _num("Outdated"),
        "dirty": _num(r"Dirty \(uncommitted changes\)"),
        "errors": _num("Errors"),
        "outdated_repos": [{"name": n, "behind": int(b)} for n, b in outdated],
        "no_repos": "No git repositories found" in logs,
        "completed": "GitHub status check completed" in logs,
    }


# ─── Помощни ───────────────────────────────────────────────────────────────

def _endpoint_id(cfg: dict) -> int:
    sup, px = cfg["supervisor"], cfg["portainer"]
    return int(sup.get("endpoint_id") or px.get("endpoint_id") or DEFAULT_ENDPOINT_ID)


def _resolve_transport(cfg: dict) -> tuple[str, str | None]:
    """(transport, sidecar_name). transport ∈ {sidecar, oneshot}.

    `supervisor.transport`: auto (по подразбиране) | sidecar | oneshot.
    При auto наличието на `supervisor.sidecar` избира sidecar пътя."""
    sup = cfg["supervisor"]
    sidecar = (sup.get("sidecar") or "").strip() or None
    want = (sup.get("transport") or "auto").strip().lower()
    if want == "sidecar":
        return "sidecar", sidecar
    if want == "oneshot":
        return "oneshot", None
    return ("sidecar", sidecar) if sidecar else ("oneshot", None)


def _dispatch(cfg: dict, eid: int, image: str, cmd: list[str],
              binds: list[str], network: str | None) -> dict:
    """Изпълнява cmd по конфигурирания транспорт."""
    transport, sidecar = _resolve_transport(cfg)
    if transport == "sidecar":
        if not sidecar:
            return {"error": "no_sidecar",
                    "detail": "transport=sidecar requires `supervisor.sidecar` "
                              "(container name) on the alias"}
        return _run_sidecar(cfg["portainer"], eid, sidecar, cmd)
    return _run_oneshot(cfg["portainer"], eid, image, cmd, binds, network)


def _resolve_mode(mode: str) -> tuple[list[str] | None, str | None]:
    """fail-closed: непознат режим → (None, error)."""
    flags = _MODE_FLAGS.get(mode)
    if flags is None:
        return None, f"unknown mode '{mode}' (allowed: {sorted(_MODE_FLAGS)})"
    return flags, None


def _build_cmd(cfg: dict, flags: list[str]) -> tuple[list[str], str]:
    conf = cfg["supervisor"].get("conf_path") or DEFAULT_CONF
    return (["supervisor", conf, *flags], conf)


# ─── Public ops ─────────────────────────────────────────────────────────────

def supervisor_status(target: str) -> dict:
    """READ-ONLY: пуска `supervisor <conf> --github-status` в еднократен контейнер
    през Portainer и връща обобщение за git drift-а на addons репата."""
    cfg = _get_supervisor_config(target)
    if not cfg["found"]:
        return {"error": "unknown_target", "target": target}
    if not cfg["portainer"].get("url"):
        return {"error": "no_portainer", "target": target,
                "detail": "add a `portainer` block (url, token) to this alias"}
    if not cfg["supervisor"]:
        return {"error": "no_supervisor_config", "target": target,
                "detail": "add a `supervisor` block (image, conf_path, binds) to this alias"}
    flags, _ = _resolve_mode("status")
    cmd, conf = _build_cmd(cfg, flags)
    image = cfg["supervisor"].get("image") or DEFAULT_IMAGE
    binds = cfg["supervisor"].get("binds") or []
    network = cfg["supervisor"].get("network") or "bridge"
    eid = _endpoint_id(cfg)

    res = _dispatch(cfg, eid, image, cmd, binds, network)
    if res.get("error"):
        return {"target": target, "mode": "status", **res}
    status = _parse_status(res.get("logs", ""))
    record = {"target": target, "mode": "status", "kind": "status",
              "exit_code": res.get("exit_code"), "image": image,
              "transport": res.get("transport"),
              "result": "ok" if res.get("exit_code") == 0 else "nonzero_exit",
              "ts": int(time.time())}
    _ops_append(record)
    return {"target": target, "mode": "status", "exit_code": res.get("exit_code"),
            "status": status, "container": res.get("container"),
            "transport": res.get("transport"),
            "logs_tail": res.get("logs", "")[-2000:]}


def supervisor_run(target: str, mode: str = "status",
                   dry_run: bool | None = None) -> dict:
    """Пуска supervisor в даден режим. Разрушителните режими (oca/ee/force/init)
    са DRY-RUN по подразбиране — реален run иска dry_run=false (и
    MCP_SUPERVISOR_DRY_RUN=0). `status` е read-only → делегира на supervisor_status."""
    if mode in _READONLY_MODES:
        return supervisor_status(target)

    cfg = _get_supervisor_config(target)
    if not cfg["found"]:
        return {"error": "unknown_target", "target": target}
    flags, err = _resolve_mode(mode)
    if err:
        return {"error": "bad_mode", "detail": err}
    if not cfg["portainer"].get("url") or not cfg["supervisor"]:
        return {"error": "not_configured", "target": target,
                "detail": "alias needs `portainer` + `supervisor` blocks"}

    # read_only alias → само read-only режими
    if cfg["portainer"].get("read_only") and mode not in _READONLY_MODES:
        return {"error": "read_only_target", "target": target,
                "detail": f"alias portainer.read_only=true blocks mode '{mode}'"}

    image = cfg["supervisor"].get("image") or DEFAULT_IMAGE
    binds = cfg["supervisor"].get("binds") or []
    network = cfg["supervisor"].get("network") or "bridge"
    eid = _endpoint_id(cfg)
    cmd, conf = _build_cmd(cfg, flags)

    transport, sidecar = _resolve_transport(cfg)
    is_dry = DRY_RUN if dry_run is None else bool(dry_run)
    if mode in _DESTRUCTIVE_MODES and is_dry:
        # ПЛАН само — нищо не се пуска.
        return {"target": target, "mode": mode, "dry_run": True,
                "plan": {"image": image, "cmd": cmd, "binds": binds,
                         "endpoint_id": eid, "network": network,
                         "transport": transport, "sidecar": sidecar},
                "note": ("destructive mode — DRY-RUN. Re-run with dry_run=false "
                         "AND MCP_SUPERVISOR_DRY_RUN=0 to execute. No per-action "
                         "TOTP exists; admin-principal gate + this dry-run are the guard.")}

    res = _dispatch(cfg, eid, image, cmd, binds, network)
    record = {"target": target, "mode": mode, "kind": "run",
              "exit_code": res.get("exit_code"), "image": image,
              "dry_run": is_dry, "transport": res.get("transport", transport),
              "result": "ok" if res.get("exit_code") == 0 else "error",
              "ts": int(time.time())}
    _ops_append(record)
    return {"target": target, "mode": mode, "dry_run": is_dry,
            "exit_code": res.get("exit_code"), "container": res.get("container"),
            "transport": res.get("transport", transport),
            "logs_tail": res.get("logs", "")[-4000:], "error": res.get("error")}


def supervisor_sidecar_status(target: str) -> dict:
    """READ-ONLY: жив ли е конфигурираният sidecar за този alias.

    Sidecar-ът НЕ се вдига оттук по замисъл: зад Cloudflare `containers/start`
    е блокиран (ADR-0002), затова стартът е еднократна ръчна операция. При
    липсващ/спрян контейнер връщаме готовия `docker run` за оператора."""
    cfg = _get_supervisor_config(target)
    if not cfg["found"]:
        return {"error": "unknown_target", "target": target}
    if not cfg["portainer"].get("url"):
        return {"error": "no_portainer", "target": target}
    transport, sidecar = _resolve_transport(cfg)
    image = cfg["supervisor"].get("image") or DEFAULT_IMAGE
    binds = cfg["supervisor"].get("binds") or []
    if not sidecar:
        return {"target": target, "transport": transport, "sidecar": None,
                "detail": "no `supervisor.sidecar` configured for this alias"}

    eid = _endpoint_id(cfg)
    base, headers = _portainer_base(cfg["portainer"])
    dbase = f"{base}/api/endpoints/{eid}/docker"
    if httpx is None:
        return {"error": "dependency_missing", "detail": "httpx not installed"}
    with httpx.Client(timeout=HTTP_TIMEOUT, verify=False) as client:
        try:
            info = _inspect_container(client, dbase, headers, sidecar)
        except Exception as e:
            return {"error": "portainer_error", "detail": str(e)[:300]}

    out = {"target": target, "transport": transport, "sidecar": sidecar,
           "endpoint_id": eid, "found": info["found"], "running": info["running"],
           "image": info.get("image")}
    if not info["running"]:
        bind_flags = " ".join(f"-v {b}" for b in binds)
        out["bootstrap"] = (
            f"docker run -d --name {sidecar} --restart unless-stopped "
            f"{bind_flags} {image} sleep infinity")
        out["note"] = ("start this once on the host (or via Portainer UI) — the "
                       "MCP cannot start containers on Cloudflare-fronted hosts "
                       "(Docker >= 29 rejects the re-chunked bodyless POST /start)")
    return out


def supervisor_history(target: str | None = None) -> dict:
    ops = _ops_replay()
    if target:
        ops = [o for o in ops if o.get("target") == target]
    return {"count": len(ops), "ops": ops[-50:]}


# ─── MCP tool surface ───────────────────────────────────────────────────────

def get_admin_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(
            name="supervisor_status",
            description=("ADMIN: read-only status of a stack's Odoo addon git repos "
                         "via the supervisor toolbox (`--github-status`) run one-shot "
                         "through Portainer. No changes are made."),
            inputSchema={
                "type": "object",
                "properties": {"target": {"type": "string",
                                          "description": "connection alias"}},
                "required": ["target"],
            },
        ),
        Tool(
            name="supervisor_run",
            description=("ADMIN: run the supervisor in a given mode via Portainer "
                         "(status|github_update|github_sync|oca|ee|force|init). "
                         "Destructive modes are DRY-RUN by default (plan only) unless "
                         "dry_run=false and MCP_SUPERVISOR_DRY_RUN=0."),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "mode": {"type": "string",
                             "enum": ["status", "github_update", "github_sync",
                                      "oca", "ee", "force", "init"],
                             "default": "status"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="supervisor_sidecar_status",
            description=("ADMIN: read-only check whether the alias's persistent "
                         "supervisor sidecar container is running. Returns the "
                         "one-time `docker run` bootstrap line when it is not — "
                         "the MCP cannot start containers behind Cloudflare."),
            inputSchema={
                "type": "object",
                "properties": {"target": {"type": "string",
                                          "description": "connection alias"}},
                "required": ["target"],
            },
        ),
        Tool(
            name="supervisor_history",
            description="ADMIN: list past supervisor ops (optionally per target).",
            inputSchema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
            },
        ),
    ]


ADMIN_TOOL_NAMES = {"supervisor_status", "supervisor_run", "supervisor_history",
                    "supervisor_sidecar_status"}


def handle(name: str, arguments: dict | None) -> dict:
    arguments = arguments or {}
    if name == "supervisor_status":
        return supervisor_status(arguments.get("target", ""))
    if name == "supervisor_run":
        return supervisor_run(arguments.get("target", ""),
                              mode=arguments.get("mode", "status"),
                              dry_run=arguments.get("dry_run"))
    if name == "supervisor_sidecar_status":
        return supervisor_sidecar_status(arguments.get("target", ""))
    if name == "supervisor_history":
        return supervisor_history(arguments.get("target"))
    return {"error": f"unknown supervisor tool: {name}"}
