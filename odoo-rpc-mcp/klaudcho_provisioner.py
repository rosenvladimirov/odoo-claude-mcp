"""Изпълнителят на Клаудчо — външните стъпки на услугите от поръчките (ADR telegram-consult-bot/0009).

Odoo (модулът klaudcho_service) държи машината на стъпките. Тук се правят само външните:

* ``telegram`` — групата от акаунта на оператора (единственият Telethon клиент на odoo-rpc,
  eager publisher-а — втори клиент на същия телефон отнема ключа, 21.07.2026), ботът — администратор;
* ``container`` — стек в Portainer от ``klaudcho_stack.template.yml``, чака контейнерът да върви;
* ``teardown`` — спира стека. Томът с дневника на агента остава.

Взима задачите с ``klaudcho_provision_pending`` и връща резултата с ``klaudcho_provision_report``.
Буди се от ``POST /klaudcho/wake`` (Odoo след commit; само подкана, без данни) и на всеки
KLAUDCHO_PROV_POLL секунди за всеки случай. Не е websocket към Docker — ADR mcp-supervisor-remote/0001.

Идемпотентност: всеки външен резултат се записва в локален дневник ПРЕДИ отчета. Ако
отчетът се загуби, следващият цикъл го праща пак, без да прави втора група или стек.

По подразбиране е DRY RUN (като provisioning_engine): нищо истинско не се създава,
докато KLAUDCHO_PROV_LIVE не е "1".
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("klaudcho_provisioner")

TEMPLATE_FILE = Path(__file__).parent / "klaudcho_stack.template.yml"
MODEL = "klaudcho.service"
ENV_KEYS = (
    "IMAGE", "NETWORK", "KNOWLEDGE_DIR", "ODOO_API_KEY", "ANTHROPIC_API_KEY",
)


def render_stack(job: dict, env: dict) -> str:
    """Шаблонът със стойностите на задачата и средата. Липсваща стойност е грешка, не празно."""
    values = {
        "SLUG": job["slug"],
        "AGENT_TOML_B64": base64.b64encode(job["agent_toml"].encode()).decode(),
        "CONTROL_TOKEN": job["control_token"],
        "READ_TOKEN": job["read_token"],
        **{k: env.get(k, "") for k in ENV_KEYS},
    }
    missing = sorted(k for k, v in values.items() if not v)
    if missing:
        raise ValueError(f"missing values for the stack: {missing}")
    text = TEMPLATE_FILE.read_text(encoding="utf-8")
    for key, value in values.items():
        # Стойностите отиват в YAML; кавички и нов ред биха го счупили — токените ги нямат
        if any(c in value for c in "\n\"'"):
            raise ValueError(f"unsafe value for {key}")
        text = text.replace("${" + key + "}", value)
    if "${" in text:
        raise ValueError("unreplaced placeholder in the stack template")
    return text


class Journal:
    """Локалният дневник на външните резултати: ключ ``<service>:<step>`` → резултатът."""

    def __init__(self, path: str):
        self.path = Path(path)

    def get(self, key: str):
        if not self.path.exists():
            return None
        found = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("key") == key:
                found = rec
        return found

    def put(self, key: str, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "ts": time.time(), **data}, ensure_ascii=False) + "\n")
        os.chmod(self.path, 0o600)


class Provisioner:
    """Един цикъл: чакащите задачи от Odoo → външната стъпка → отчет."""

    def __init__(self, odoo, tg=None, portainer=None, journal: Journal | None = None,
                 env: dict | None = None, live: bool = False, clock=time.sleep):
        self.odoo = odoo                  # .execute_kw(model, method, args, kwargs)
        self.tg = tg                      # callable → TelegramServiceManager или None
        self.portainer = portainer        # обект с create_stack / delete_stack / container_running
        self.journal = journal or Journal("/data/klaudcho_provision.jsonl")
        self.env = env or {}
        self.live = live
        self.sleep = clock
        self.last_run: dict = {}
        self._lock = threading.Lock()      # нишката и ръчното пускане не вървят едновременно

    # -- Odoo -----------------------------------------------------------
    def _pending(self) -> list:
        return self.odoo.execute_kw(MODEL, "klaudcho_provision_pending", [], {}) or []

    def _report(self, job: dict, ok: bool, data=None, error=None):
        return self.odoo.execute_kw(MODEL, "klaudcho_provision_report", [[job["service_id"]], job["step"], ok],
                                    {"data": data or {}, "error": error})

    # -- цикълът --------------------------------------------------------
    def run_once(self) -> list:
        with self._lock:
            return self._run_once()

    def _run_once(self) -> list:
        results = []
        for job in self._pending():
            key = f"{job['service_id']}:{job['step']}"
            done = self.journal.get(key)
            try:
                if done and done.get("ok"):
                    data = done.get("data") or {}     # направено, отчетът се е загубил — само го пращаме
                else:
                    data = self._do(job)
                    self.journal.put(key, {"ok": True, "data": data})
                state = self._report(job, True, data)
                results.append({"key": key, "ok": True, "state": state})
            except Exception as e:  # noqa: BLE001 — грешката отива в Odoo, не убива цикъла
                logger.warning("klaudcho provision %s failed: %s", key, e)
                try:
                    state = self._report(job, False, error=f"{type(e).__name__}: {e}"[:500])
                except Exception as e2:  # noqa: BLE001
                    state = f"report failed: {e2}"
                results.append({"key": key, "ok": False, "state": state, "error": str(e)[:200]})
        self.last_run = {"ts": time.time(), "results": results}
        return results

    def _do(self, job: dict) -> dict:
        step = job["step"]
        if step == "telegram":
            return self._telegram(job)
        if step == "container":
            return self._container(job)
        if step == "teardown":
            return self._teardown(job)
        raise ValueError(f"unknown step {step}")

    def _telegram(self, job: dict) -> dict:
        if not self.live:
            return {"chat_id": "-100" + str(1000000000 + int(job["service_id"])), "invite_link": "", "dry_run": True}
        mgr = self.tg() if self.tg else None
        if mgr is None:
            raise RuntimeError("no Telegram publisher (TELEGRAM_EAGER_PRINCIPALS)")
        res = mgr.create_group(job["title"], list(job.get("members") or []), bot_username=job.get("bot_username", ""))
        if not res.get("chat_id"):
            raise RuntimeError(f"group not created: {res}")
        if job.get("bot_username") and not res.get("bot_added"):
            # Без бота агентът и дежурството не виждат групата — по-добре грешка сега
            raise RuntimeError(f"group {res['chat_id']} created, but the bot was not added")
        return {"chat_id": res["chat_id"], "invite_link": res.get("invite_link") or "",
                "failed_members": res.get("failed") or []}

    def _container(self, job: dict) -> dict:
        compose = render_stack(job, self.env)
        if not self.live:
            return {"stack": job["slug"], "dry_run": True}
        res = self.portainer.create_stack(job["slug"], compose)
        if res.get("error") and "already exists" not in json.dumps(res):
            raise RuntimeError(f"portainer: {res}")
        timeout = int(self.env.get("START_TIMEOUT") or 120)
        waited = 0
        while waited < timeout:
            if self.portainer.container_running(job["slug"]):
                return {"stack": job["slug"]}
            self.sleep(5)
            waited += 5
        raise TimeoutError(f"{job['slug']} is not running after {timeout}s")

    def _teardown(self, job: dict) -> dict:
        if not self.live:
            return {"stack": job["slug"], "dry_run": True}
        res = self.portainer.delete_stack(job["slug"])
        if res.get("error"):
            raise RuntimeError(f"portainer: {res}")
        return {"stack": job["slug"]}


class Portainer:
    """Тънка обвивка над provisioning_engine + проверката „контейнерът върви ли“."""

    def __init__(self, engine, url: str, api_key: str, endpoint_id: str):
        self.engine = engine
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.endpoint_id = endpoint_id

    def create_stack(self, name, compose):
        return self.engine.portainer_create_stack(name, compose)

    def delete_stack(self, name):
        return self.engine.portainer_delete_stack(name)

    def container_running(self, name) -> bool:
        import httpx
        r = httpx.get(f"{self.url}/api/endpoints/{self.endpoint_id}/docker/containers/{name}/json",
                      headers={"X-API-Key": self.api_key}, timeout=20)
        if r.status_code != 200:
            return False
        state = (r.json() or {}).get("State") or {}
        return bool(state.get("Running")) and not state.get("Restarting")


# ----------------------------------------------------------------------
# Връзка със server.py: нишката, будилникът, админ инструментът
# ----------------------------------------------------------------------
_wake = threading.Event()
_instance: Provisioner | None = None


def wake() -> None:
    _wake.set()


def build_from_env(odoo_connection_cls, tg_getter, engine) -> Provisioner | None:
    """Изпълнителят от средата; без KLAUDCHO_PROV_ODOO_URL е изключен."""
    e = os.environ
    url = e.get("KLAUDCHO_PROV_ODOO_URL", "").strip()
    if not url:
        return None
    odoo = odoo_connection_cls(alias="klaudcho-provisioner", url=url, db=e.get("KLAUDCHO_PROV_ODOO_DB", ""),
                               username=e.get("KLAUDCHO_PROV_ODOO_LOGIN", ""),
                               api_key=e.get("KLAUDCHO_PROV_ODOO_API_KEY", ""))
    portainer = Portainer(engine, e.get("MCP_PORTAINER_URL", ""), e.get("MCP_PORTAINER_API_KEY", ""),
                          e.get("MCP_PORTAINER_ENDPOINT_ID", "2"))
    env = {k: e.get(f"KLAUDCHO_PROV_{k}", "") for k in ENV_KEYS}
    env["START_TIMEOUT"] = e.get("KLAUDCHO_PROV_START_TIMEOUT", "120")
    return Provisioner(odoo, tg=tg_getter, portainer=portainer,
                       journal=Journal(e.get("KLAUDCHO_PROV_JOURNAL", "/data/klaudcho_provision.jsonl")),
                       env=env, live=e.get("KLAUDCHO_PROV_LIVE", "0") == "1")


def start(provisioner: Provisioner, poll: int | None = None) -> None:
    global _instance
    _instance = provisioner
    poll = poll or int(os.environ.get("KLAUDCHO_PROV_POLL", "300"))

    def loop():
        time.sleep(15)            # Telegram клиентът се вдига 8 s след старта
        while True:
            _wake.clear()
            try:
                provisioner.run_once()
            except Exception as e:  # noqa: BLE001 — Odoo може да е паднал; пак след малко
                logger.warning("klaudcho provisioner cycle failed: %s", e)
            _wake.wait(poll)

    threading.Thread(target=loop, name="klaudcho-provisioner", daemon=True).start()
    logger.info("klaudcho provisioner started (live=%s, poll=%ss)", provisioner.live, poll)


async def wake_asgi(scope, receive, send) -> None:
    """POST /klaudcho/wake — подкана от Odoo. Тайната: KLAUDCHO_PROV_WAKE_TOKEN (X-Klaudcho-Wake)."""
    async def reply(status: int, obj: dict):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(obj).encode()})

    if scope.get("method") != "POST":
        return await reply(405, {"error": "method_not_allowed"})
    secret = os.environ.get("KLAUDCHO_PROV_WAKE_TOKEN", "")
    if not secret or _instance is None:
        return await reply(503, {"error": "provisioner_disabled"})
    hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    if not hmac.compare_digest(hdrs.get("x-klaudcho-wake", ""), secret):
        return await reply(401, {"error": "unauthorized"})
    wake()
    return await reply(200, {"ok": True})


# -- админ инструментът (по образеца на health_monitor) -------------------
ADMIN_TOOL_NAMES = {"klaudcho_provision_status"}


def get_admin_tools() -> list:
    from mcp.types import Tool
    return [Tool(
        name="klaudcho_provision_status",
        description="ADMIN: Klaudcho provisioner — state (live/dry-run), the last cycle, "
                    "and optionally run one cycle now (run=true).",
        inputSchema={"type": "object", "properties": {"run": {"type": "boolean", "default": False}}},
    )]


def handle(name: str, arguments: dict | None) -> dict:
    if name != "klaudcho_provision_status":
        return {"error": f"unknown tool {name}"}
    if _instance is None:
        return {"enabled": False, "hint": "set KLAUDCHO_PROV_ODOO_URL/DB/LOGIN/API_KEY"}
    if (arguments or {}).get("run"):
        _instance.run_once()
    return {"enabled": True, "live": _instance.live, "last_run": _instance.last_run}
