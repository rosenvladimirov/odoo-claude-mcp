"""
Odoo RPC MCP Server — Full XML-RPC/JSON-RPC access to Odoo.

Supports:
- Multiple Odoo connections (by alias)
- Auth: user/password or user/apikey
- XML-RPC (Odoo 8+) and JSON-RPC (Odoo 14+)
- Full CRUD: search, read, search_read, create, write, unlink
- execute_kw: call any model method
- fields_get: model introspection
- report: generate PDF reports
- SSE/HTTP and Streamable HTTP transport for Docker deployment

Transport: Streamable HTTP (recommended) or SSE/HTTP fallback
"""
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import uuid
import unicodedata
import xmlrpc.client
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool

from google_service import GoogleServiceManager
from telegram_service import TelegramServiceManager

import ai_usage_log
import ai_vision_service
import ai_invoice_engine

# ─── MCP Proxy (client for sub-services) ────────────────────

PROXY_CONFIG_FILE = Path(os.environ.get("PROXY_CONFIG_FILE", "/data/proxy_services.json"))
PROXY_CONFIG_ENV = os.environ.get("PROXY_SERVICES_JSON", "")


def _proxy_services():
    """Load proxy config from JSON file, env var, or defaults."""
    # 1. Try config file
    if PROXY_CONFIG_FILE.is_file():
        try:
            with open(PROXY_CONFIG_FILE, "r", encoding="utf-8") as f:
                services = json.load(f)
            logger.info(f"Proxy config loaded from {PROXY_CONFIG_FILE}: {list(services.keys())}")
            # Expand env vars in headers
            for svc in services.values():
                if "headers" in svc:
                    svc["headers"] = {
                        k: os.path.expandvars(v) for k, v in svc["headers"].items()
                    }
            return services
        except Exception as e:
            logger.warning(f"Proxy config file error: {e}")

    # 2. Try env var (JSON string)
    if PROXY_CONFIG_ENV:
        try:
            services = json.loads(PROXY_CONFIG_ENV)
            logger.info(f"Proxy config from PROXY_SERVICES_JSON: {list(services.keys())}")
            return services
        except Exception as e:
            logger.warning(f"Proxy config env error: {e}")

    # 3. Defaults (backwards compatible)
    github_token = os.environ.get("GITHUB_TOKEN", "")
    return {
        "portainer": {"transport": "sse", "url": "http://portainer-mcp:8085/sse"},
        "github": {
            "transport": "http",
            "url": "http://github-mcp:8086/mcp",
            "headers": {"Authorization": f"Bearer {github_token}"} if github_token else {},
        },
        "teams": {"transport": "sse", "url": "http://teams-mcp:8087/sse"},
    }

PROXY_SERVICES = None  # lazy init
PROXY_TOOLS: dict[str, dict] = {}  # "portainer__listStacks" -> {"service": "portainer", "tool": "listStacks"}
PROXY_TOOL_DEFS: list[Tool] = []  # Tool definitions for proxied tools


def _get_proxy_services():
    global PROXY_SERVICES
    if PROXY_SERVICES is None:
        PROXY_SERVICES = _proxy_services()
    return PROXY_SERVICES


def _discover_proxy_tools(only_services: set | None = None):
    """Connect to sub-services and register their tools with prefixed names.
    Merges with existing discoveries — never loses previously found tools."""
    global PROXY_TOOLS, PROXY_TOOL_DEFS
    services = _get_proxy_services()
    if only_services:
        services = {k: v for k, v in services.items() if k in only_services}

    for svc_name, svc_config in services.items():
        try:
            raw_tools = _proxy_list_tools(svc_name)
            if raw_tools and not any("error" in t for t in raw_tools):
                # Remove old tools from this service
                PROXY_TOOLS = {k: v for k, v in PROXY_TOOLS.items() if v["service"] != svc_name}
                PROXY_TOOL_DEFS = [t for t in PROXY_TOOL_DEFS if not t.name.startswith(f"{svc_name}__")]
                # Add new
                for t in raw_tools:
                    prefixed = f"{svc_name}__{t['name']}"
                    PROXY_TOOLS[prefixed] = {"service": svc_name, "tool": t["name"]}
                    PROXY_TOOL_DEFS.append(Tool(
                        name=prefixed,
                        description=f"[{svc_name}] {t.get('description', t['name'])}",
                        inputSchema=t.get("inputSchema", {"type": "object", "properties": {}}),
                    ))
                logger.info(f"Proxy: discovered {len(raw_tools)} tools from {svc_name}")
            else:
                logger.warning(f"Proxy: {svc_name} returned no tools or error")
        except Exception as e:
            logger.warning(f"Proxy: failed to discover {svc_name}: {e}")

    logger.info(f"Proxy: total {len(PROXY_TOOLS)} proxied tools registered")


def _proxy_call_isolated(service: str, tool_name: str, arguments: dict) -> Any:
    """Run proxy call in an isolated thread with its own event loop."""
    import concurrent.futures
    def _run():
        return asyncio.run(_async_proxy_call_impl(service, tool_name, arguments))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result(timeout=60)


async def _async_proxy_call_impl(service: str, tool_name: str, arguments: dict) -> Any:
    """Actual async implementation — runs in a fresh event loop."""
    services = _get_proxy_services()
    if service not in services:
        return {"error": f"Unknown service: {service}"}
    svc = services[service]
    transport_type = svc["transport"]
    url = svc["url"]
    headers = svc.get("headers", {})

    try:
        if transport_type == "sse":
            from mcp.client.sse import sse_client
            from mcp import ClientSession
            async with sse_client(url, headers=headers, timeout=60, sse_read_timeout=120) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
        else:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
            async with streamablehttp_client(url, headers=headers, timeout=60) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)

        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
        combined = "\n".join(texts) if texts else str(result)
        try:
            return json.loads(combined)
        except (json.JSONDecodeError, TypeError):
            return {"result": combined}
    except BaseException as e:
        detail = str(e)
        if hasattr(e, 'exceptions'):
            detail = "; ".join(f"{type(sub).__name__}: {sub}" for sub in e.exceptions)
        return {"error": f"Proxy {service}/{tool_name}: {detail}"}


async def _async_proxy_call(service: str, tool_name: str, arguments: dict) -> Any:
    """Proxy call — SSE via subprocess (isolated process), HTTP via MCP client."""
    services = _get_proxy_services()
    if service not in services:
        return {"error": f"Unknown service: {service}"}
    svc = services[service]
    transport_type = svc["transport"]
    url = svc["url"]
    headers = svc.get("headers", {})

    try:
        if transport_type == "sse":
            # SSE supergateway requires isolated process (event loop conflict with uvicorn)
            return await asyncio.get_event_loop().run_in_executor(
                None, _subprocess_sse_call, url, tool_name, arguments)
        else:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
            async with streamablehttp_client(url, headers=headers, timeout=60) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
            texts = [item.text for item in result.content if hasattr(item, "text")]
            combined = "\n".join(texts) if texts else str(result)
            try:
                return json.loads(combined)
            except (json.JSONDecodeError, TypeError):
                return {"result": combined}
    except BaseException as e:
        detail = str(e)
        if hasattr(e, 'exceptions'):
            detail = "; ".join(f"{type(sub).__name__}: {sub}" for sub in e.exceptions)
        logger.error(f"Proxy {service}/{tool_name}: {detail}")
        return {"error": f"Proxy {service}/{tool_name}: {detail}"}


def _subprocess_sse_call(url: str, tool_name: str, arguments: dict) -> Any:
    """Call SSE MCP tool via subprocess — fully isolated event loop."""
    import subprocess
    script = f'''
import asyncio, json, sys
from mcp.client.sse import sse_client
from mcp import ClientSession

async def main():
    async with sse_client({url!r}, timeout=60, sse_read_timeout=120) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool({tool_name!r}, {json.dumps(arguments)})
            texts = [item.text for item in result.content if hasattr(item, "text")]
            print(json.dumps({{"result": "\\n".join(texts)}}))

asyncio.run(main())
'''
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return {"error": f"SSE subprocess: {proc.stderr.strip()[-500:]}"}
        output = proc.stdout.strip()
        if not output:
            return {"error": "SSE subprocess: no output"}
        data = json.loads(output)
        text = data.get("result", "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"result": text}
    except subprocess.TimeoutExpired:
        return {"error": "SSE subprocess: timeout"}
    except Exception as e:
        return {"error": f"SSE subprocess: {str(e)}"}


def _proxy_call(service: str, tool_name: str, arguments: dict) -> Any:
    """Forward a tool call to an internal MCP sub-service."""
    services = _get_proxy_services()
    if service not in services:
        return {"error": f"Unknown service: {service}"}
    svc = services[service]
    transport_type = svc["transport"]
    url = svc["url"]
    headers = svc.get("headers", {})

    async def _do_call():
        if transport_type == "sse":
            from mcp.client.sse import sse_client
            from mcp import ClientSession
            async with sse_client(url, headers=headers, timeout=60, sse_read_timeout=120) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return result
        else:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
            async with streamablehttp_client(url, headers=headers, timeout=60) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return result

    import concurrent.futures
    import traceback

    def _run():
        return asyncio.run(_do_call())

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_run).result(timeout=60)
        texts = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
        combined = "\n".join(texts) if texts else str(result)
        try:
            return json.loads(combined)
        except (json.JSONDecodeError, TypeError):
            return {"result": combined}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Proxy {service}/{tool_name} error:\n{tb}")
        # Try to extract sub-exceptions from ExceptionGroup
        detail = str(e)
        if hasattr(e, 'exceptions'):
            detail = "; ".join(str(sub) for sub in e.exceptions)
        return {"error": f"Proxy {service}/{tool_name}: {detail}"}


def _proxy_list_tools(service: str) -> list[dict]:
    """List tools available on an internal MCP sub-service."""
    services = _get_proxy_services()
    if service not in services:
        return []
    svc = services[service]
    transport_type = svc["transport"]
    url = svc["url"]
    headers = svc.get("headers", {})

    async def _do_list():
        if transport_type == "sse":
            from mcp.client.sse import sse_client
            from mcp import ClientSession
            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return result.tools
        else:
            from mcp.client.streamable_http import streamablehttp_client
            from mcp import ClientSession
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return result.tools

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        try:
            tools = pool.submit(lambda: asyncio.run(_do_list())).result(timeout=30)
            return [{"name": t.name, "description": t.description,
                     "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {}} for t in tools]
        except Exception as e:
            return [{"error": str(e)}]


# ─── Configuration ──────────────────────────────────────────
MCP_PORT = int(os.environ.get("MCP_PORT", "8084"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
CONNECTIONS_FILE = Path(os.environ.get("CONNECTIONS_FILE", "/data/connections.json"))
SESSIONS_DB = Path(os.environ.get("SESSIONS_DB", "/data/sessions.db"))
# Single-connection mode: hide disconnect, skip file loading, force alias "default"
SINGLE_CONNECTION = os.environ.get("SINGLE_CONNECTION", "").lower() in ("1", "true", "yes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("odoo-rpc-mcp")


# ─── Fiscal Position Reference Data (l10n_bg_tax_admin) ───
FP_MOVE_TYPES = [
    ["entry", "Journal Entry"],
    ["out_invoice", "Customer Invoice"],
    ["out_refund", "Customer Credit Note"],
    ["in_invoice", "Vendor Bill"],
    ["in_refund", "Vendor Credit Note"],
    ["out_receipt", "Sales Receipt"],
    ["in_receipt", "Purchase Receipt"],
]

FP_BG_MOVE_TYPES = [
    ["standard", "Standard"],
    ["customs", "Customs"],
    ["invoice_customs", "Invoice include in customs"],
    ["private", "Private"],
    ["protocol", "Protocol"],
]

FP_DOC_TYPES = [
    ["01", "Invoice"],
    ["02", "Debit note"],
    ["03", "Credit note"],
    ["04", "Storeable goods sent to EU"],
    ["05", "Storeable goods receive from EU"],
    ["07", "Customs declarations"],
    ["09", "Protocols or other"],
    ["11", "Invoice - cash reporting"],
    ["12", "Debit notice - cash reporting"],
    ["13", "Credit notice - cash statement"],
    ["50", "Protocols fuel supplies"],
    ["81", "Sales report - tickets"],
    ["82", "Special tax order"],
    ["83", "Sales of bread"],
    ["84", "Sales of flour"],
    ["23", "Credit note art. 126b"],
    ["29", "Protocol under Art. 126b"],
    ["91", "Protocol under Art. 151c"],
    ["92", "Protocol under Art. 151g"],
    ["93", "Protocol under Art. 151c"],
    ["94", "Protocol under Art. 151c, para. 7"],
    ["95", "Protocol for free provision of foodstuffs"],
]

FP_TYPE_VAT = [
    ["standard", "Accounting document"],
    ["117_protocol_82_2", "(SER) Art. 117, para. 1, item 1 — Art. 82, para. 2, item 3"],
    ["117_protocol_84", "(ICD) Art. 117, para. 1, item 1 — Art. 84"],
    ["117_protocol_6_4", "(DON) Art. 117 — Art. 6, para. 4"],
    ["117_protocol_6_3", "(PRIV) Art. 117 — Art. 6, para. 3"],
    ["117_protocol_15", "(TRI) Art. 117 — Art. 15"],
    ["117_protocol_82_2_2", "(TER) Art. 117 — Art. 82, para. 2, item 2"],
    ["119_report", "Art. 119 - Report for sales"],
    ["in_customs", "Import Customs declaration"],
    ["out_customs", "Export Customs declaration"],
]

FP_ACTION_FIELDS = [
    "id", "position_id", "move_type", "l10n_bg_move_type",
    "l10n_bg_type_vat", "l10n_bg_document_type", "l10n_bg_narration",
    "dest_move_type", "position_dest_id", "account_id",
    "partner_id", "factor_percent",
]


# ─── Odoo RPC Client ───────────────────────────────────────
class OdooConnection:
    """Manages a single Odoo RPC connection."""

    def __init__(
        self,
        alias: str,
        url: str,
        db: str,
        username: str,
        password: str = "",
        api_key: str = "",
        protocol: str = "xmlrpc",
        verify_ssl: bool = True,
    ):
        self.alias = alias
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.password = password
        self.api_key = api_key
        self.protocol = protocol  # xmlrpc or jsonrpc
        self.verify_ssl = verify_ssl
        self._uid: int | None = None
        self._auth_token: str = ""  # password or api_key
        self._ssl_ctx_cache: "ssl.SSLContext | None" = None

    @property
    def auth_token(self) -> str:
        return self.api_key or self.password

    # ── SSL / self-signed handling ──────────────────────────────
    #
    # When a client runs its own Odoo behind a self-signed cert, default
    # Python CA verification rejects it. Rather than globally disabling
    # verification (MITM vulnerable), we use trust-on-first-use (TOFU):
    #   • first connect with verify_ssl=False → fetch the peer cert,
    #     persist to /data/ssl_certs/<alias>.pem
    #   • subsequent connects build an SSL context that trusts ONLY that
    #     specific cert — so if the peer cert changes we fail closed
    # This matches the MCP filestore pattern the user asked for.

    def _ssl_certs_dir(self) -> Path:
        d = Path(os.environ.get("MCP_SSL_CERTS_DIR", "/data/ssl_certs"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cached_cert_path(self) -> Path:
        safe = _sanitize_name(self.alias)
        return self._ssl_certs_dir() / f"{safe}.pem"

    def _fetch_and_cache_cert(self) -> Path:
        """Download peer certificate chain and persist it for future use."""
        from urllib.parse import urlparse
        parsed = urlparse(self.url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host or parsed.scheme != "https":
            raise ValueError(
                f"Cannot fetch cert for non-HTTPS URL: {self.url}"
            )
        # ssl.get_server_certificate does the TLS handshake and returns the
        # peer's cert PEM. We wrap it in a permissive context because the
        # whole point of fetching is that we don't yet trust anyone.
        import ssl as _ssl
        pem = _ssl.get_server_certificate((host, port))
        path = self._cached_cert_path()
        path.write_text(pem)
        logger.info(
            f"[{self.alias}] Cached self-signed cert from {host}:{port} → {path}"
        )
        return path

    def _get_ssl_context(self):
        """Return an SSL context appropriate for this connection."""
        import ssl as _ssl
        if self._ssl_ctx_cache is not None:
            return self._ssl_ctx_cache
        if self.verify_ssl:
            ctx = _ssl.create_default_context()
        else:
            path = self._cached_cert_path()
            if not path.exists():
                try:
                    self._fetch_and_cache_cert()
                except Exception as e:
                    logger.warning(
                        f"[{self.alias}] Cert fetch failed, falling back to "
                        f"no-verify: {e}"
                    )
                    ctx = _ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = _ssl.CERT_NONE
                    self._ssl_ctx_cache = ctx
                    return ctx
            # Build context that trusts ONLY the cached cert (pinning).
            ctx = _ssl.create_default_context(cafile=str(path))
            # Self-signed certs frequently use IPs / internal names whose
            # SAN doesn't match; disable hostname check but keep cert chain
            # validation against our pinned cert.
            ctx.check_hostname = False
        self._ssl_ctx_cache = ctx
        return ctx

    def authenticate(self) -> int:
        """Authenticate via XML-RPC and return uid."""
        if self._uid is not None:
            return self._uid

        if self.api_key:
            # API key auth: still need to resolve uid via authenticate
            # In Odoo 14+, api_key works as password in XML-RPC
            self._auth_token = self.api_key
        else:
            self._auth_token = self.password

        try:
            transport = _UASafeTransport(context=self._get_ssl_context()) \
                if self.url.startswith("https") else _UATransport()
            common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common",
                allow_none=True,
                transport=transport,
            )
            self._uid = common.authenticate(
                self.db, self.username, self._auth_token, {}
            )
            if not self._uid:
                raise Exception(
                    f"Authentication failed for {self.username}@{self.url}/{self.db}"
                )
            logger.info(f"[{self.alias}] Authenticated as uid={self._uid}")
            return self._uid
        except Exception as e:
            self._uid = None
            raise Exception(f"[{self.alias}] Auth error: {e}")

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        """Execute any Odoo model method via XML-RPC."""
        uid = self.authenticate()
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}

        try:
            if self.protocol == "jsonrpc":
                return self._jsonrpc_call(model, method, args, kwargs)
            else:
                transport = _UASafeTransport(context=self._get_ssl_context()) \
                    if self.url.startswith("https") else _UATransport()
                obj = xmlrpc.client.ServerProxy(
                    f"{self.url}/xmlrpc/2/object",
                    allow_none=True,
                    transport=transport,
                )
                return obj.execute_kw(
                    self.db, uid, self.auth_token, model, method, args, kwargs
                )
        except xmlrpc.client.Fault as e:
            raise Exception(f"Odoo RPC fault: {e.faultString}")

    def _jsonrpc_call(
        self, model: str, method: str, args: list, kwargs: dict
    ) -> Any:
        """Execute via JSON-RPC (synchronous, uses requests-like approach)."""
        import urllib.request

        uid = self.authenticate()
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "call",
            "id": 1,
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [self.db, uid, self.auth_token, model, method, args, kwargs],
            },
        }).encode()

        req = urllib.request.Request(
            f"{self.url}/jsonrpc",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        ctx = self._get_ssl_context() if self.url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            result = json.loads(resp.read())

        if "error" in result:
            err = result["error"]
            raise Exception(f"JSON-RPC error: {err.get('message', err)}")
        return result.get("result")

    def to_dict(self) -> dict:
        return {
            "alias": self.alias,
            "url": self.url,
            "db": self.db,
            "username": self.username,
            "protocol": self.protocol,
            "verify_ssl": self.verify_ssl,
            "pinned_cert": (
                str(self._cached_cert_path())
                if not self.verify_ssl and self._cached_cert_path().exists()
                else ""
            ),
            "has_api_key": bool(self.api_key),
            "has_password": bool(self.password),
        }


# ─── Connection Manager ────────────────────────────────────
class SessionManager:
    """
    Tracks active Claude terminal sessions.

    Each Claude terminal window registers itself on startup with its
    opened Odoo context (model, res_id, view_type).  When MCP tools
    modify data, the session registry is used to notify the correct
    user/window via bus notifications so the UI can refresh live.

    Stored in SQLite so multiple concurrent Claude windows can each
    have their own entry.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    connection_alias TEXT,
                    odoo_url TEXT,
                    odoo_db TEXT,
                    odoo_username TEXT,
                    model TEXT,
                    res_id INTEGER DEFAULT 0,
                    view_type TEXT,
                    terminal_url TEXT,
                    created_at TEXT,
                    last_activity TEXT
                )
                """
            )
            conn.commit()

    def register(
        self,
        connection_alias: str = "default",
        odoo_url: str = "",
        odoo_db: str = "",
        odoo_username: str = "",
        model: str = "",
        res_id: int = 0,
        view_type: str = "",
        terminal_url: str = "",
    ) -> str:
        session_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions
                (session_id, connection_alias, odoo_url, odoo_db, odoo_username,
                 model, res_id, view_type, terminal_url, created_at, last_activity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, connection_alias, odoo_url, odoo_db, odoo_username,
                    model, int(res_id or 0), view_type, terminal_url, now, now,
                ),
            )
            conn.commit()
        logger.info(
            f"Session registered: {session_id} — {odoo_url} {model}({res_id})"
        )
        return session_id

    def update_context(self, session_id: str, model: str = None, res_id: int = None, view_type: str = None):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT model, res_id, view_type FROM sessions WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            if not row:
                return False
            new_model = model if model is not None else row[0]
            new_res_id = int(res_id) if res_id is not None else row[1]
            new_view_type = view_type if view_type is not None else row[2]
            conn.execute(
                "UPDATE sessions SET model = ?, res_id = ?, view_type = ?, last_activity = ? WHERE session_id = ?",
                (new_model, new_res_id, new_view_type, now, session_id),
            )
            conn.commit()
        return True

    def touch(self, session_id: str):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET last_activity = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()

    def get(self, session_id: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_by_connection(self, connection_alias: str) -> list[dict]:
        """Return all sessions using a given connection alias, most recent first."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sessions WHERE connection_alias = ? ORDER BY last_activity DESC",
                (connection_alias,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY last_activity DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, session_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            conn.commit()
        return cur.rowcount > 0

    def cleanup_stale(self, max_age_hours: int = 24) -> int:
        """Remove sessions with no activity for max_age_hours."""
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)
        cutoff_iso = datetime.fromtimestamp(cutoff).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE last_activity < ?", (cutoff_iso,)
            )
            conn.commit()
        return cur.rowcount


class ConnectionManager:
    """Manages multiple Odoo connections."""

    def __init__(self, data_file: Path):
        self.data_file = data_file
        self.connections: dict[str, OdooConnection] = {}
        self._load()

    def _load(self):
        """Load connections from file and environment."""
        # From environment (single connection shortcut)
        url = os.environ.get("ODOO_URL", "")
        if url:
            self.connections["default"] = OdooConnection(
                alias="default",
                url=url,
                db=os.environ.get("ODOO_DB", ""),
                username=os.environ.get("ODOO_USERNAME", os.environ.get("ODOO_USER", "")),
                password=os.environ.get("ODOO_PASSWORD", ""),
                api_key=os.environ.get("ODOO_API_KEY", ""),
                protocol=os.environ.get("ODOO_PROTOCOL", "xmlrpc"),
            )

        # From connections file (single-connection: only load "default")
        if self.data_file.exists():
            try:
                data = json.loads(self.data_file.read_text())
                for item in data if isinstance(data, list) else data.get("connections", []):
                    if SINGLE_CONNECTION and item.get("alias") != "default":
                        continue
                    conn = OdooConnection(
                        alias=item["alias"],
                        url=item["url"],
                        db=item["db"],
                        username=item["username"],
                        password=item.get("password", ""),
                        api_key=item.get("api_key", ""),
                        protocol=item.get("protocol", "xmlrpc"),
                        verify_ssl=bool(item.get("verify_ssl", True)),
                    )
                    self.connections[conn.alias] = conn
            except Exception as e:
                logger.warning(f"Failed to load connections: {e}")

    def _save(self):
        """Persist connections (without secrets in plaintext — only metadata)."""
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for conn in self.connections.values():
            item = {
                "alias": conn.alias,
                "url": conn.url,
                "db": conn.db,
                "username": conn.username,
                "protocol": conn.protocol,
                "verify_ssl": conn.verify_ssl,
            }
            # Store credentials (user accepts risk for local Docker)
            if conn.password:
                item["password"] = conn.password
            if conn.api_key:
                item["api_key"] = conn.api_key
            data.append(item)
        self.data_file.write_text(json.dumps(data, indent=2))

    def add(self, **kwargs) -> OdooConnection:
        conn = OdooConnection(**kwargs)
        self.connections[conn.alias] = conn
        self._save()
        return conn

    def remove(self, alias: str) -> bool:
        if alias in self.connections:
            del self.connections[alias]
            self._save()
            return True
        return False

    def get(self, alias: str = "default") -> OdooConnection:
        if alias not in self.connections:
            if len(self.connections) == 1:
                return next(iter(self.connections.values()))
            raise Exception(
                f"Connection '{alias}' not found. "
                f"Available: {list(self.connections.keys())}"
            )
        return self.connections[alias]

    def list_all(self) -> list[dict]:
        return [c.to_dict() for c in self.connections.values()]


# ─── MCP Server ─────────────────────────────────────────────
manager: ConnectionManager | None = None
session_mgr: SessionManager | None = None
google_mgr: GoogleServiceManager | None = None
telegram_mgr: TelegramServiceManager | None = None
mcp_server = Server("odoo-rpc-mcp")


def _mgr() -> ConnectionManager:
    if manager is None:
        raise Exception("Connection manager not initialized")
    return manager


def _notify_live_refresh(conn: OdooConnection, kind: str, model: str, res_ids: list, values: dict = None) -> None:
    """
    Send a live-refresh bus notification to Odoo so the user's open
    form/list view reflects Claude's changes immediately.

    kind: "field" (field-level refresh in form) or "list" (new row in list)
    """
    if session_mgr is None:
        return
    sessions = session_mgr.find_by_connection(conn.alias)
    if not sessions:
        return
    payload = {
        "kind": kind,
        "model": model,
        "res_ids": res_ids if isinstance(res_ids, list) else [res_ids],
        "values": values or {},
        "sessions": [
            {
                "session_id": s["session_id"],
                "model": s["model"],
                "res_id": s["res_id"],
                "view_type": s["view_type"],
            }
            for s in sessions
        ],
    }
    try:
        method = (
            "notify_claude_refresh_field"
            if kind == "field"
            else "notify_claude_refresh_list"
        )
        conn.execute_kw("res.users", method, [payload])
    except Exception as e:
        logger.warning(f"Live refresh notify failed ({kind}): {e}")


def _md_to_html(text: str) -> str:
    """Convert Markdown text to Odoo-safe HTML."""
    try:
        import markdown as _md
        return _md.markdown(
            text,
            extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
        )
    except ImportError:
        import html as _html
        escaped = _html.escape(text)
        paragraphs = escaped.split("\n\n")
        return "".join(
            f'<p>{p.replace(chr(10), "<br/>")}</p>' for p in paragraphs
        )


def _conn(args: dict) -> OdooConnection:
    return _mgr().get(args.get("connection", "default"))


# ─── Web Session Manager ─────────────────────────────────

import requests as http_requests

_web_sessions: dict[str, "OdooWebSession"] = {}


class OdooWebSession:
    """Persistent Odoo web session with cookie-based authentication."""

    def __init__(self, url: str, db: str, login: str, password: str):
        self.url = url.rstrip("/")
        self.db = db
        self.login = login
        self.password = password
        self.session = http_requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.uid = None
        self.session_info = None
        self._rpc_id = 0

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def authenticate(self) -> dict:
        """Login via /web/session/authenticate, stores session_id cookie."""
        resp = self.session.post(
            f"{self.url}/web/session/authenticate",
            json={
                "jsonrpc": "2.0", "method": "call",
                "params": {"db": self.db, "login": self.login, "password": self.password},
                "id": self._next_id(),
            },
            verify=False, timeout=30,
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", str(data["error"])))
        result = data.get("result", {})
        self.uid = result.get("uid")
        if not self.uid:
            raise RuntimeError("Authentication failed: no uid returned")
        self.session_info = result
        return result

    @property
    def authenticated(self) -> bool:
        return self.uid is not None

    @property
    def session_id(self) -> str:
        return self.session.cookies.get("session_id", "")

    def _jsonrpc(self, url: str, params: dict, timeout: int = 60) -> Any:
        """Execute JSON-RPC call with session cookie."""
        if not self.authenticated:
            self.authenticate()
        resp = self.session.post(
            f"{self.url}{url}",
            json={
                "jsonrpc": "2.0", "method": "call",
                "params": params,
                "id": self._next_id(),
            },
            verify=False, timeout=timeout,
        )
        data = resp.json()
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            if code == 100:  # Session expired
                self.authenticate()
                return self._jsonrpc(url, params, timeout)
            raise RuntimeError(err.get("data", {}).get("message", err.get("message", str(err))))
        return data.get("result")

    def call_kw(self, model: str, method: str, args: list = None, kwargs: dict = None) -> Any:
        """Call model method via /web/dataset/call_kw."""
        return self._jsonrpc("/web/dataset/call_kw", {
            "model": model, "method": method,
            "args": args or [], "kwargs": kwargs or {},
        })

    def web_read(self, model: str, domain: list, fields: list,
                 limit: int = 80, offset: int = 0, order: str = "") -> Any:
        """Search+read via /web/dataset/call_kw (frontend format)."""
        return self._jsonrpc("/web/dataset/call_kw", {
            "model": model, "method": "web_search_read",
            "args": [],
            "kwargs": {
                "domain": domain, "fields": fields,
                "limit": limit, "offset": offset, "order": order,
            },
        })

    def export_data(self, model: str, domain: list, fields: list,
                    import_compat: bool = False) -> Any:
        """Export data via /web/export/csv."""
        # First get record IDs
        ids = self.call_kw(model, "search", [domain])
        return self.call_kw(model, "export_data", [ids, fields], {
            "import_compat": import_compat,
        })

    def get_report_pdf(self, report_name: str, ids: list) -> bytes:
        """Download PDF report via /report/pdf."""
        if not self.authenticated:
            self.authenticate()
        ids_str = ",".join(str(i) for i in ids)
        resp = self.session.get(
            f"{self.url}/report/pdf/{report_name}/{ids_str}",
            verify=False, timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Report download failed: HTTP {resp.status_code}")
        return resp.content

    def get_binary(self, model: str, record_id: int, field: str = "datas") -> bytes:
        """Download binary field via /web/content."""
        if not self.authenticated:
            self.authenticate()
        resp = self.session.get(
            f"{self.url}/web/content/{model}/{record_id}/{field}",
            verify=False, timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Binary download failed: HTTP {resp.status_code}")
        return resp.content

    def raw_request(self, path: str, method: str = "GET",
                    data: dict = None, params: dict = None) -> dict:
        """Raw HTTP request to any controller URL."""
        if not self.authenticated:
            self.authenticate()
        url = f"{self.url}{path}"
        if method.upper() == "GET":
            resp = self.session.get(url, params=params, verify=False, timeout=60)
        else:
            resp = self.session.post(
                url, json=data or params, verify=False, timeout=60,
            )
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return {"status": resp.status_code, "json": resp.json()}
        return {
            "status": resp.status_code,
            "content_type": content_type,
            "body": resp.text[:5000],
            "size": len(resp.content),
        }

    def destroy(self):
        """Logout via /web/session/destroy."""
        try:
            self._jsonrpc("/web/session/destroy", {})
        except Exception:
            pass
        self.uid = None
        self.session_info = None

    def to_dict(self) -> dict:
        return {
            "url": self.url, "db": self.db, "login": self.login,
            "uid": self.uid, "authenticated": self.authenticated,
            "session_id": self.session_id[:12] + "..." if self.session_id else None,
        }


def _get_web_session(args: dict) -> OdooWebSession:
    """Get or create web session from connection alias."""
    alias = args.get("connection", "default")
    if alias in _web_sessions and _web_sessions[alias].authenticated:
        return _web_sessions[alias]
    raise RuntimeError(f"No active web session for '{alias}'. Call odoo_web_login first.")


# ─── Per-user storage ──────────────────────────────────────

# In-memory: mcp_session_id -> user_name
_session_users: dict[str, str] = {}

# Per-async-task user context (isolates concurrent MCP sessions)
import contextvars
_current_user_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
REPOS_DIR = os.environ.get("REPOS_DIR", "/repos")


def _parse_fs_module(mod_path: str, manifest_path: str, source: str,
                     repo: str, instance: str) -> dict:
    """Parse a module from filesystem for odoo_module_info."""
    import ast as _ast
    info = {
        "source": source,
        "repo": repo,
        "instance": instance,
        "path": mod_path,
    }
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = _ast.literal_eval(f.read())
        info["version"] = data.get("version", "")
        info["license"] = data.get("license", "")
        info["summary"] = data.get("summary", data.get("description", ""))[:200]
        info["depends"] = data.get("depends", [])
        info["auto_install"] = data.get("auto_install", False)
        info["installable"] = data.get("installable", True)
        info["category"] = data.get("category", "")
        info["author"] = data.get("author", "")
        info["application"] = data.get("application", False)
        info["countries"] = data.get("countries", [])
        ext = data.get("external_dependencies", {})
        if ext:
            info["external_dependencies"] = ext
    except Exception as e:
        info["parse_error"] = str(e)
    return info


_CYR_TO_LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
    "ь": "", "ю": "yu", "я": "ya", "є": "ye", "і": "i", "ї": "yi",
    "ґ": "g", "ё": "yo", "э": "e", "ы": "y",
})


def _sanitize_name(name: str) -> str:
    """Transliterate to ASCII and sanitize for use as directory name."""
    # Cyrillic transliteration first, then NFKD for accented Latin
    text = name.lower().translate(_CYR_TO_LAT)
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in ascii_name)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "user"


# ─── AI Invoice — tenant helpers ─────────────────────────

def _ai_tenant_code(conn, override: str | None = None) -> str:
    """Derive tenant slug from connection db, unless caller supplies one."""
    if override:
        return _sanitize_name(override)
    return _sanitize_name(conn.db or conn.alias or "default")


def _ai_tenant_credentials(tenant_code: str) -> tuple[str, str]:
    """Return (api_key, base_url) for tenant.

    Resolution order:
      1. Per-tenant env: ANTHROPIC_API_KEY_<TENANT_UPPER>
      2. Global env:     ANTHROPIC_API_KEY
    Base URL (defaults to Anthropic direct):
      1. Per-tenant env: ANTHROPIC_BASE_URL_<TENANT_UPPER>
      2. Global env:     ANTHROPIC_BASE_URL (e.g. CF AI Gateway)
      3. Default:        https://api.anthropic.com
    """
    slug = tenant_code.upper().replace("-", "_").replace(".", "_")
    api_key = (
        os.environ.get(f"ANTHROPIC_API_KEY_{slug}")
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )
    base_url = (
        os.environ.get(f"ANTHROPIC_BASE_URL_{slug}")
        or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    )
    return api_key, base_url


def _ai_write_back_to_move(conn, move_id: int, data: dict, attachment_id: int) -> dict:
    """Apply extracted JSON to account.move — cautious, only draft, only empty fields.

    Writes to chatter always (audit trail). Attempts to fill `partner_id`,
    `invoice_date`, `ref` if missing and the draft state permits. Does NOT
    touch invoice_line_ids — those require unit/tax matching which belongs in
    the skill (l10n_bg_ai_invoice_glue._skill_post_vendor_bill).
    """
    import json as _json
    info: dict = {"attempted": True, "fields_written": [], "skipped": []}
    try:
        move_recs = conn.execute_kw(
            "account.move", "read", [[move_id]],
            {"fields": ["state", "move_type", "partner_id", "invoice_date", "ref"]},
        )
        if not move_recs:
            info["error"] = f"move {move_id} not found"
            return info
        move = move_recs[0]
        if move["state"] != "draft":
            info["skipped"].append(f"state={move['state']} not draft")
            return info

        writes: dict = {}
        # Partner by VAT (only fill if empty)
        if not move.get("partner_id") and data.get("partner_vat"):
            vat = data["partner_vat"].replace(" ", "").upper()
            partner_ids = conn.execute_kw(
                "res.partner", "search", [[["vat", "=", vat]]], {"limit": 1}
            )
            if partner_ids:
                writes["partner_id"] = partner_ids[0]
            else:
                info["skipped"].append(f"partner VAT {vat} not found")

        # Invoice date
        if not move.get("invoice_date") and data.get("invoice_date"):
            writes["invoice_date"] = data["invoice_date"]

        # Vendor reference (invoice_number)
        if not move.get("ref") and data.get("invoice_number"):
            writes["ref"] = data["invoice_number"]

        if writes:
            conn.execute_kw("account.move", "write", [[move_id], writes])
            info["fields_written"] = list(writes.keys())

        # Always post to chatter for audit
        try:
            body_lines = [
                "<b>🤖 AI Extracted Invoice Data</b>",
                f"Attachment: #{attachment_id}",
                "<pre style='font-size:11px;white-space:pre-wrap'>"
                + _json.dumps(data, indent=2, ensure_ascii=False)[:3000]
                + "</pre>",
            ]
            conn.execute_kw(
                "account.move", "message_post", [[move_id]],
                {"body": "".join(body_lines), "message_type": "comment",
                 "subtype_xmlid": "mail.mt_note"},
            )
            info["chatter_posted"] = True
        except Exception as e:  # noqa: BLE001
            info["chatter_error"] = str(e)[:200]
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def _list_existing_users() -> list[str]:
    """List existing user profile directories."""
    users_dir = os.path.join(DATA_DIR, "users")
    if not os.path.isdir(users_dir):
        return []
    return sorted(
        d for d in os.listdir(users_dir)
        if os.path.isdir(os.path.join(users_dir, d))
    )


def _user_dir(user_name: str, create: bool = False) -> str:
    """Get per-user data directory. Only creates if create=True."""
    safe = _sanitize_name(user_name)
    d = os.path.join(DATA_DIR, "users", safe)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _user_connections_file(user_name: str) -> str:
    return os.path.join(_user_dir(user_name), "connections.json")


def _user_active_file(user_name: str) -> str:
    return os.path.join(_user_dir(user_name), "active_connection.json")


def _load_user_connections(user_name: str) -> dict:
    fpath = _user_connections_file(user_name)
    if os.path.exists(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_user_connections(user_name: str, conns: dict):
    _user_dir(user_name, create=True)
    with open(_user_connections_file(user_name), "w", encoding="utf-8") as f:
        json.dump(conns, f, indent=2, ensure_ascii=False)


def _load_user_active(user_name: str) -> dict:
    fpath = _user_active_file(user_name)
    if os.path.exists(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_user_active(user_name: str, active: dict):
    _user_dir(user_name, create=True)
    with open(_user_active_file(user_name), "w", encoding="utf-8") as f:
        json.dump(active, f, indent=2, ensure_ascii=False)


def _get_mcp_session_key() -> str:
    """Get unique key for the current MCP session (per-client isolation)."""
    try:
        ctx = mcp_server.request_context
        return str(id(ctx.session))
    except (LookupError, AttributeError):
        return "default"


def _get_current_user(args: dict) -> str | None:
    """Resolve the current MCP user for the running request.

    Security invariant (task 4): this function NEVER reads identity from
    ``args``. Callers of memory_* / user_connection_* tools cannot pass a
    ``user`` argument and address another profile — identity comes only
    from the HTTP-validated ContextVar or from the per-session slot set
    by ``identify()``.

    Priority:
      0. HTTP-validated Odoo caller (unified-auth, task 2).
         ContextVar ``_odoo_caller_ctx`` is set by the ASGI middleware
         after successful XMLRPC validation + connection lookup.
      1. Per-session user bound by ``identify()`` tool call (stdio).
      2. Backward-compat "current" slot (single-session legacy).

    Returns None if no identity is bound — tools should then error with
    "Call identify(name) first" or "Use unified-auth headers".
    """
    caller = _odoo_caller_ctx.get()
    if caller:
        return caller["mcp_user"]
    key = _get_mcp_session_key()
    if key in _session_users:
        return _session_users[key]
    if "current" in _session_users:
        return _session_users["current"]
    return None


# ─── Odoo API-key authentication middleware (unified auth, task 2) ──
import threading as _auth_threading
import hashlib as _auth_hashlib
import time as _auth_time
import ssl as _auth_ssl

# cache_key (sha256) → (mcp_user, login, uid, expires_at)
_auth_cache: dict[str, tuple[str, str, int, float]] = {}
_auth_cache_lock = _auth_threading.Lock()
AUTH_CACHE_TTL = int(os.environ.get("AUTH_CACHE_TTL", "300"))  # 5 minutes default

# Per-async-task validated Odoo caller. Set by ASGI middleware,
# read by MCP tool handlers via _get_current_user.
_odoo_caller_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "odoo_caller", default=None,
)


def _resolve_mcp_user(url: str, db: str, login: str, api_key: str) -> tuple[str, str] | None:
    """Find the MCP user whose connections.json contains exactly this 4-tuple.

    Returns (mcp_user_safe_name, alias) or None if no profile owns this key.
    """
    norm_url = url.rstrip("/")
    users_dir = os.path.join(DATA_DIR, "users")
    if not os.path.isdir(users_dir):
        return None
    for mcp_user in sorted(os.listdir(users_dir)):
        conns_file = os.path.join(users_dir, mcp_user, "connections.json")
        if not os.path.isfile(conns_file):
            continue
        try:
            with open(conns_file, "r", encoding="utf-8") as f:
                conns = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for alias, c in (conns or {}).items():
            if (c.get("url", "").rstrip("/") == norm_url
                    and c.get("db") == db
                    and c.get("user") == login
                    and c.get("api_key") == api_key):
                return mcp_user, alias
    return None


_XMLRPC_UA = "OdooMcpAuth/1.0 (+https://mcp.odoo-shell.space)"


class _UATransport(xmlrpc.client.Transport):
    """HTTP XMLRPC transport with a non-default User-Agent."""
    user_agent = _XMLRPC_UA


class _UASafeTransport(xmlrpc.client.SafeTransport):
    """HTTPS XMLRPC transport with a non-default User-Agent.

    Default ``Python-xmlrpc/3.x`` is blocked by Cloudflare Bot Fight Mode
    on many Odoo deployments behind CF. A legitimate-looking UA lets
    ``common.authenticate`` reach the origin server.
    """
    user_agent = _XMLRPC_UA


def _xmlrpc_validate(url: str, db: str, login: str, api_key: str) -> int | None:
    """Validate (login, api_key) against Odoo XMLRPC. Returns uid or None."""
    try:
        if url.lower().startswith("https://"):
            ctx = _auth_ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _auth_ssl.CERT_NONE
            transport = _UASafeTransport(context=ctx)
        else:
            transport = _UATransport()
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common",
                                           transport=transport)
        uid = common.authenticate(db, login, api_key, {})
        return uid if uid else None
    except Exception as e:
        logger.warning(f"[AUTH] XMLRPC validate failed for {login}@{url}/{db}: {e}")
        return None


def get_caller_odoo_user(headers: dict) -> dict | None:
    """Resolve calling MCP user from HTTP headers.

    Required headers (bytes-keyed, as ASGI scope["headers"]):
        Authorization: Bearer <api_key>
        X-Odoo-Url:    <https://...>
        X-Odoo-Db:     <db>
        X-Odoo-Login:  <login>

    Optional ENV: ALLOWED_ODOO_URLS (comma-separated whitelist).

    Returns dict {mcp_user, alias, url, db, login, uid} or None.
    """
    auth = headers.get(b"authorization", b"").decode().strip()
    if not auth.lower().startswith("bearer "):
        return None
    api_key = auth[7:].strip()
    url = headers.get(b"x-odoo-url", b"").decode().strip().rstrip("/")
    db = headers.get(b"x-odoo-db", b"").decode().strip()
    login = headers.get(b"x-odoo-login", b"").decode().strip()
    if not all([api_key, url, db, login]):
        return None

    allowed_env = os.environ.get("ALLOWED_ODOO_URLS", "").strip()
    if allowed_env:
        allowed = {u.strip().rstrip("/") for u in allowed_env.split(",") if u.strip()}
        if url not in allowed:
            logger.warning(f"[AUTH] Rejected non-whitelisted Odoo URL: {url}")
            return None

    cache_key = _auth_hashlib.sha256(f"{url}|{db}|{login}|{api_key}".encode()).hexdigest()
    now = _auth_time.time()

    with _auth_cache_lock:
        cached = _auth_cache.get(cache_key)
        if cached and cached[3] > now:
            mcp_user, cached_login, uid, _exp = cached
            resolved = _resolve_mcp_user(url, db, login, api_key)
            alias = resolved[1] if resolved else "?"
            return {
                "mcp_user": mcp_user, "alias": alias,
                "url": url, "db": db, "login": cached_login, "uid": uid,
            }

    uid = _xmlrpc_validate(url, db, login, api_key)
    if not uid:
        return None

    resolved = _resolve_mcp_user(url, db, login, api_key)
    if not resolved:
        logger.warning(
            f"[AUTH] Valid Odoo key but no registered MCP user for "
            f"{login}@{url}/{db}. Use POST /api/user/register-connection first."
        )
        return None
    mcp_user, alias = resolved

    with _auth_cache_lock:
        _auth_cache[cache_key] = (mcp_user, login, uid, now + AUTH_CACHE_TTL)
        if len(_auth_cache) > 1000:
            for k, v in list(_auth_cache.items()):
                if v[3] <= now:
                    _auth_cache.pop(k, None)

    return {
        "mcp_user": mcp_user, "alias": alias,
        "url": url, "db": db, "login": login, "uid": uid,
    }


# ─── Memory storage helpers ─────────────────────────────────

MEMORY_DIR = os.path.join(DATA_DIR, "memory")


def _memory_shared_dir(create: bool = False) -> str:
    d = os.path.join(MEMORY_DIR, "shared")
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _memory_user_dir(user_name: str, create: bool = False) -> str:
    safe = _sanitize_name(user_name)
    d = os.path.join(MEMORY_DIR, "users", safe)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _memory_licensed_dir(tenant_code: str, create: bool = False) -> str:
    """Per-tenant licensed memory store.

    Files here come from purchased `ai.billing.memory.pack` records
    deployed via the admin API. Visible only to callers resolving to the
    same tenant_code.
    """
    safe = _sanitize_name(tenant_code)
    d = os.path.join(MEMORY_DIR, "licensed", safe)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _memory_list_files(directory: str) -> list[dict]:
    """List .md files in a directory with metadata."""
    results = []
    if not os.path.isdir(directory):
        return results
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(directory, fname)
        stat = os.stat(fpath)
        # Read frontmatter for description
        description = ""
        mem_type = ""
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read(2048)
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    fm = content[3:end]
                    for line in fm.strip().splitlines():
                        if line.startswith("description:"):
                            description = line.split(":", 1)[1].strip()
                        elif line.startswith("type:"):
                            mem_type = line.split(":", 1)[1].strip()
        results.append({
            "filename": fname,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "description": description,
            "type": mem_type,
        })
    return results


# ─── Tool Definitions ──────────────────────────────────────

TOOLS = [
    # ── Connection management ──
    Tool(
        name="odoo_connect",
        description=(
            "Add/update an Odoo connection. Auth via password or API key. "
            "Protocol: xmlrpc (Odoo 8+) or jsonrpc (Odoo 14+)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Connection name (e.g. 'production', 'staging')", "default": "default"},
                "url": {"type": "string", "description": "Odoo URL (e.g. http://localhost:8069)"},
                "db": {"type": "string", "description": "Database name"},
                "username": {"type": "string", "description": "Login username"},
                "password": {"type": "string", "description": "Password (or leave empty if using api_key)", "default": ""},
                "api_key": {"type": "string", "description": "API key (Odoo 14+, alternative to password)", "default": ""},
                "protocol": {"type": "string", "enum": ["xmlrpc", "jsonrpc"], "default": "xmlrpc"},
                "verify_ssl": {
                    "type": "boolean",
                    "description": (
                        "True = standard CA verification (default). "
                        "False = allow self-signed. On first use the peer "
                        "cert is fetched and pinned under "
                        "/data/ssl_certs/<alias>.pem for subsequent calls "
                        "(trust-on-first-use). Set False for on-prem clients "
                        "with private CA or self-issued certs."
                    ),
                    "default": True,
                },
            },
            "required": ["url", "db", "username"],
        },
    ),
    Tool(
        name="odoo_cert_info",
        description=(
            "Return the pinned SSL certificate details (issuer, subject, "
            "notAfter, fingerprint) for a connection. Useful to verify which "
            "self-signed cert MCP has trusted. Requires verify_ssl=False on "
            "the connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "default": "default"},
            },
        },
    ),
    Tool(
        name="odoo_cert_refresh",
        description=(
            "Re-fetch the peer SSL certificate for a connection and overwrite "
            "the pinned copy. Use after the server's self-signed cert was "
            "rotated. Fails if the connection has verify_ssl=True (standard "
            "CA verification does not need pinning)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "default": "default"},
            },
        },
    ),
    Tool(
        name="odoo_disconnect",
        description="Remove an Odoo connection.",
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "default": "default"},
            },
        },
    ),
    Tool(
        name="odoo_connections",
        description="List all configured Odoo connections.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ── Introspection ──
    Tool(
        name="odoo_list_models",
        description="List available Odoo models (ir.model). Optionally filter by name pattern.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "pattern": {"type": "string", "description": "Filter by model name (e.g. 'sale', 'account')", "default": ""},
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="odoo_fields_get",
        description="Get field definitions for an Odoo model. Returns field names, types, labels, and attributes.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name (e.g. 'res.partner')"},
                "attributes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Field attributes to return (e.g. ['string', 'type', 'required', 'relation'])",
                    "default": ["string", "type", "required", "readonly", "relation"],
                },
            },
            "required": ["model"],
        },
    ),
    # ── CRUD ──
    Tool(
        name="odoo_search",
        description="Search for record IDs matching a domain filter.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name"},
                "domain": {
                    "type": "array",
                    "description": "Odoo domain filter (e.g. [['is_company','=',true]])",
                    "default": [],
                },
                "limit": {"type": "integer", "default": 80},
                "offset": {"type": "integer", "default": 0},
                "order": {"type": "string", "description": "Sort order (e.g. 'name asc, id desc')", "default": ""},
            },
            "required": ["model"],
        },
    ),
    Tool(
        name="odoo_read",
        description="Read specific records by IDs.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}, "description": "Record IDs to read"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fields to return (empty = all)",
                    "default": [],
                },
            },
            "required": ["model", "ids"],
        },
    ),
    Tool(
        name="odoo_search_read",
        description="Search and read records in one call. Most common operation.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "domain": {"type": "array", "default": []},
                "fields": {"type": "array", "items": {"type": "string"}, "default": []},
                "limit": {"type": "integer", "default": 80},
                "offset": {"type": "integer", "default": 0},
                "order": {"type": "string", "default": ""},
            },
            "required": ["model"],
        },
    ),
    Tool(
        name="odoo_search_count",
        description="Count records matching a domain.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "domain": {"type": "array", "default": []},
            },
            "required": ["model"],
        },
    ),
    Tool(
        name="odoo_create",
        description="Create one or more records. Returns list of new IDs.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "values": {
                    "type": ["object", "array"],
                    "description": "Field values dict, or list of dicts for batch create",
                },
            },
            "required": ["model", "values"],
        },
    ),
    Tool(
        name="odoo_write",
        description="Update existing records.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "values": {"type": "object", "description": "Field values to update"},
            },
            "required": ["model", "ids", "values"],
        },
    ),
    Tool(
        name="odoo_unlink",
        description="Delete records by IDs.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["model", "ids"],
        },
    ),
    # ── Generic execute ──
    Tool(
        name="odoo_execute",
        description=(
            "Execute any model method via execute_kw. "
            "Use for workflow actions (action_confirm, action_done), "
            "custom methods, or anything not covered by CRUD tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "method": {"type": "string", "description": "Method name (e.g. 'action_confirm', 'button_validate')"},
                "args": {"type": "array", "description": "Positional arguments", "default": []},
                "kwargs": {"type": "object", "description": "Keyword arguments", "default": {}},
            },
            "required": ["model", "method"],
        },
    ),
    # ── Message Post ──
    Tool(
        name="odoo_message_post",
        description=(
            "Post a message or internal note on any Odoo record (chatter). "
            "Body supports Markdown formatting (headers, bold, italic, tables, "
            "code blocks) — automatically converted to HTML. "
            "Use message_type='note' for internal notes (employees only) "
            "or 'comment' for public messages (visible to followers)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name (e.g. 'sale.order')"},
                "res_id": {"type": "integer", "description": "Record ID to post on"},
                "body": {"type": "string", "description": "Message body in Markdown format"},
                "message_type": {
                    "type": "string",
                    "enum": ["note", "comment"],
                    "default": "note",
                    "description": "note = internal (employees only), comment = public (all followers)",
                },
                "subject": {"type": "string", "description": "Message subject (optional)"},
                "partner_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Partner IDs to notify (optional)",
                },
                "attachment_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Existing ir.attachment IDs to attach (optional)",
                },
            },
            "required": ["model", "res_id", "body"],
        },
    ),
    # ── Attachment Upload ──
    Tool(
        name="odoo_attachment_upload",
        description=(
            "Upload a file as an ir.attachment on an Odoo record. "
            "Returns the attachment ID which can be used with odoo_message_post "
            "(attachment_ids parameter) or linked to any record."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name (e.g. 'sale.order')"},
                "res_id": {"type": "integer", "description": "Record ID to attach to"},
                "filename": {"type": "string", "description": "File name (e.g. 'report.pdf')"},
                "content_base64": {"type": "string", "description": "Base64-encoded file content"},
                "mimetype": {"type": "string", "description": "MIME type (optional, auto-detected)"},
            },
            "required": ["model", "res_id", "filename", "content_base64"],
        },
    ),
    Tool(
        name="odoo_attachment_download",
        description=(
            "Download an ir.attachment from Odoo by ID. "
            "Returns filename, mimetype, size, and base64-encoded content."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "attachment_id": {"type": "integer", "description": "ir.attachment record ID"},
                "save_path": {
                    "type": "string", "default": "",
                    "description": "Optional local path to save the file (instead of returning base64)",
                },
            },
            "required": ["attachment_id"],
        },
    ),
    # ── Module inspector ──
    Tool(
        name="odoo_module_info",
        description=(
            "Get detailed information about an Odoo module: Odoo RPC state + "
            "filesystem locations (OCA, EE, custom repos). Shows where the module "
            "physically exists, its manifest, dependencies, and installation status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "module": {"type": "string", "description": "Technical module name (e.g. 'account_asset')"},
            },
            "required": ["module"],
        },
    ),
    # ── Web Session (cookie-based HTTP access) ──
    Tool(
        name="odoo_web_login",
        description=(
            "Login to Odoo web interface with user/password. Creates a persistent "
            "cookie session for accessing web controllers, exports, reports, and "
            "any frontend URL. Session is reused until logout or expiry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "url": {"type": "string", "description": "Odoo URL (default: from connection)"},
                "db": {"type": "string", "description": "Database (default: from connection)"},
                "login": {"type": "string", "description": "Username/email"},
                "password": {"type": "string", "description": "Password or API key"},
            },
        },
    ),
    Tool(
        name="odoo_web_call",
        description=(
            "Call any Odoo model method via web session (JSON-RPC /web/dataset/call_kw). "
            "Works like odoo_execute but uses cookie session instead of XML-RPC."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name"},
                "method": {"type": "string", "description": "Method name"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            "required": ["model", "method"],
        },
    ),
    Tool(
        name="odoo_web_read",
        description=(
            "Search and read records via web session (frontend web_search_read format). "
            "Supports field specification, domain, limit, offset, order."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name"},
                "domain": {"type": "array", "default": []},
                "fields": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 80},
                "offset": {"type": "integer", "default": 0},
                "order": {"type": "string", "default": ""},
            },
            "required": ["model", "fields"],
        },
    ),
    Tool(
        name="odoo_web_export",
        description="Export records to structured data via web session (Odoo export_data).",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name"},
                "domain": {"type": "array", "default": []},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Field paths (e.g. 'partner_id/name')"},
                "import_compat": {"type": "boolean", "default": False},
            },
            "required": ["model", "fields"],
        },
    ),
    Tool(
        name="odoo_web_report",
        description="Download PDF report via web session. Returns base64-encoded PDF.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "report_name": {"type": "string", "description": "Report technical name"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "save_path": {"type": "string", "default": "", "description": "Save to file instead of returning base64"},
            },
            "required": ["report_name", "ids"],
        },
    ),
    Tool(
        name="odoo_web_request",
        description=(
            "Raw HTTP request to any Odoo controller URL via web session. "
            "Access frontend pages, custom controllers, website routes, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "path": {"type": "string", "description": "URL path (e.g. '/shop/cart', '/my/invoices')"},
                "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
                "data": {"type": "object", "default": {}, "description": "POST body (JSON)"},
                "params": {"type": "object", "default": {}, "description": "Query params"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="odoo_web_logout",
        description="Destroy web session and logout.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
            },
        },
    ),
    # ── Public Access (web session controller routes) ──
    Tool(
        name="public_access_export_xlsx",
        description="Export Odoo list data as XLSX file via web session. Returns base64.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name (e.g. 'sale.order')"},
                "fields": {"type": "array", "items": {"type": "string"}, "description": "Field names to export"},
                "domain": {"type": "array", "default": [], "description": "Search domain"},
                "import_compat": {"type": "boolean", "default": False},
                "save_path": {"type": "string", "default": ""},
            },
            "required": ["model", "fields"],
        },
    ),
    Tool(
        name="public_access_export_csv",
        description="Export Odoo list data as CSV via web session.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "domain": {"type": "array", "default": []},
                "import_compat": {"type": "boolean", "default": False},
                "save_path": {"type": "string", "default": ""},
            },
            "required": ["model", "fields"],
        },
    ),
    Tool(
        name="public_access_report_pdf",
        description="Download PDF report via web session. Route: /report/pdf/{report_name}/{doc_ids}.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "report_name": {"type": "string", "description": "Technical report name (e.g. 'account.report_invoice')"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "save_path": {"type": "string", "default": ""},
            },
            "required": ["report_name", "ids"],
        },
    ),
    Tool(
        name="public_access_report_html",
        description="Render report as HTML via web session.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "report_name": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["report_name", "ids"],
        },
    ),
    Tool(
        name="public_access_download",
        description="Download attachment/binary content by ID via /web/content/{id}. Public route.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "content_id": {"type": "integer", "description": "Attachment ID"},
                "save_path": {"type": "string", "default": ""},
            },
            "required": ["content_id"],
        },
    ),
    Tool(
        name="public_access_image",
        description="Download image field from record via /web/image/{model}/{id}/{field}.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string"},
                "record_id": {"type": "integer"},
                "field": {"type": "string", "default": "image_1920"},
                "save_path": {"type": "string", "default": ""},
            },
            "required": ["model", "record_id"],
        },
    ),
    Tool(
        name="public_access_barcode",
        description="Generate barcode image via /report/barcode/{type}/{value}.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "barcode_type": {"type": "string", "enum": ["Code128", "QR", "EAN13", "EAN8", "UPC-A", "Code39"], "default": "Code128"},
                "value": {"type": "string", "description": "Barcode value"},
                "width": {"type": "integer", "default": 600},
                "height": {"type": "integer", "default": 100},
                "save_path": {"type": "string", "default": ""},
            },
            "required": ["value"],
        },
    ),
    Tool(
        name="public_access_portal_home",
        description="Get portal home page content via web session (/my/home).",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
            },
        },
    ),
    Tool(
        name="public_access_portal_invoices",
        description="Get list of portal invoices (/my/invoices). Returns HTML page content.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "page": {"type": "integer", "default": 1},
            },
        },
    ),
    Tool(
        name="public_access_portal_orders",
        description="Get list of portal sale orders (/my/orders). Returns HTML page content.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "page": {"type": "integer", "default": 1},
            },
        },
    ),
    Tool(
        name="public_access_portal_purchases",
        description="Get list of portal purchase orders (/my/purchase).",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "page": {"type": "integer", "default": 1},
            },
        },
    ),
    Tool(
        name="public_access_portal_tickets",
        description="Get list of portal helpdesk tickets (/my/tickets).",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "page": {"type": "integer", "default": 1},
            },
        },
    ),
    Tool(
        name="public_access_report_xlsx",
        description="Download XLSX report via OCA reporting-engine (/report/xlsx/{name}/{ids}).",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "report_name": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "save_path": {"type": "string", "default": ""},
            },
            "required": ["report_name", "ids"],
        },
    ),
    Tool(
        name="public_access_shop",
        description="Get website shop product listing (/shop). Returns HTML.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "page": {"type": "integer", "default": 1},
                "search": {"type": "string", "default": ""},
            },
        },
    ),
    Tool(
        name="public_access_sitemap",
        description="Download sitemap.xml from Odoo website.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
            },
        },
    ),
    # ── Report ──
    Tool(
        name="odoo_report",
        description="Generate a PDF report for records. Returns base64-encoded PDF.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "report_name": {"type": "string", "description": "Report technical name (e.g. 'account.report_invoice')"},
                "ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["report_name", "ids"],
        },
    ),
    # ── Server info ──
    Tool(
        name="odoo_version",
        description="Get Odoo server version info.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
            },
        },
    ),
    # ── View Refresh (push notification to Odoo browser tab) ──
    Tool(
        name="odoo_refresh",
        description=(
            "Send a refresh notification to the user's Odoo browser tab. "
            "Call this after creating, updating, or deleting records so the "
            "user's list/form/kanban view reloads automatically. "
            "Requires l10n_bg_claude_terminal module installed on the Odoo instance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {
                    "type": "string",
                    "description": "Model name to refresh (e.g. 'sale.order'). Empty = refresh any view.",
                },
                "res_id": {
                    "type": "integer",
                    "description": "Specific record ID (0 = refresh all records in the view).",
                    "default": 0,
                },
            },
        },
    ),
    # ── Fiscal Position Configuration (l10n_bg_tax_admin) ──
    Tool(
        name="odoo_fp_list",
        description=(
            "List fiscal positions with tax action map summary (l10n_bg_tax_admin). "
            "Shows position name, auto_apply, country, company, and action mapping count."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "company_id": {"type": "integer", "description": "Filter by company ID"},
                "country_id": {"type": "integer", "description": "Filter by country ID"},
                "name": {"type": "string", "description": "Filter by name (ilike)"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="odoo_fp_details",
        description=(
            "Get detailed fiscal position with all tax action map entries (l10n_bg_tax_admin). "
            "Returns position info plus full action mappings: move types, BG move types, "
            "VAT types, document types, narrations, replacement rules, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "position_id": {"type": "integer", "description": "Fiscal position ID"},
            },
            "required": ["position_id"],
        },
    ),
    Tool(
        name="odoo_fp_configure",
        description=(
            "Add or update a tax action map entry for a fiscal position (l10n_bg_tax_admin). "
            "Provide action_id to update existing entry, or position_id to create new. "
            "Use odoo_fp_types for available selection values."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "action_id": {"type": "integer", "description": "Existing action entry ID (for update). Omit to create."},
                "position_id": {"type": "integer", "description": "Fiscal position ID (required for create)"},
                "move_type": {
                    "type": "string",
                    "enum": ["entry", "out_invoice", "out_refund", "in_invoice", "in_refund", "out_receipt", "in_receipt"],
                    "description": "Type of move",
                },
                "l10n_bg_move_type": {
                    "type": "string",
                    "enum": ["standard", "customs", "invoice_customs", "private", "protocol"],
                    "description": "Bulgarian move type",
                },
                "l10n_bg_type_vat": {
                    "type": "string",
                    "description": "VAT type code (use odoo_fp_types for values)",
                },
                "l10n_bg_document_type": {
                    "type": "string",
                    "description": "Document type code (use odoo_fp_types for values)",
                },
                "l10n_bg_narration": {
                    "type": "string",
                    "description": "Narration for audit report (required for create)",
                },
                "dest_move_type": {
                    "type": "string",
                    "enum": ["entry", "out_invoice", "out_refund", "in_invoice", "in_refund", "out_receipt", "in_receipt"],
                    "description": "Replacement move type for auto-generated documents",
                },
                "position_dest_id": {"type": "integer", "description": "Replacement fiscal position ID"},
                "account_id": {"type": "integer", "description": "Account ID for base amount of tax"},
                "partner_id": {"type": "integer", "description": "Partner ID for generated tax lines"},
                "factor_percent": {"type": "number", "description": "Factor percentage (default 100)"},
            },
        },
    ),
    Tool(
        name="odoo_fp_remove_action",
        description="Remove a tax action map entry from a fiscal position (l10n_bg_tax_admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "action_id": {"type": "integer", "description": "Tax action map entry ID to remove"},
            },
            "required": ["action_id"],
        },
    ),
    Tool(
        name="odoo_fp_types",
        description=(
            "Get available selection values for fiscal position configuration (l10n_bg_tax_admin). "
            "Returns move_types, bg_move_types, doc_types, and type_vat. "
            "Use as reference when calling odoo_fp_configure. "
            "Set live=true to fetch current values from Odoo instead of cached."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "live": {"type": "boolean", "description": "Fetch from Odoo fields_get instead of cached values", "default": False},
            },
        },
    ),
    # ── Google Services ──
    Tool(
        name="google_auth",
        description=(
            "Authenticate with Google OAuth2 for Gmail and Calendar access. "
            "Requires credentials.json from Google Cloud Console (Desktop app type). "
            "First call opens browser for consent. Token is saved for reuse."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "credentials_file": {
                    "type": "string",
                    "description": "Path to Google OAuth credentials.json (default: /data/google_credentials.json)",
                    "default": "",
                },
            },
        },
    ),
    Tool(
        name="google_auth_status",
        description="Check Google authentication status.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ── Gmail ──
    Tool(
        name="google_gmail_search",
        description=(
            "Search Gmail messages. Uses Gmail search syntax "
            "(e.g. 'from:user@example.com', 'subject:invoice', 'after:2026/01/01', 'is:unread')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "default": 10},
                "label_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Filter by label IDs (e.g. ['INBOX', 'UNREAD'])",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="google_gmail_read",
        description="Read a specific Gmail message by ID. Returns full body, headers, and labels.",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID"},
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="google_gmail_send",
        description="Send an email or reply to an existing message.",
        inputSchema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email(s), comma-separated"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Email body (plain text or HTML)"},
                "cc": {"type": "string", "default": ""},
                "bcc": {"type": "string", "default": ""},
                "html": {"type": "boolean", "description": "Send as HTML", "default": False},
                "reply_to_message_id": {
                    "type": "string",
                    "description": "Message ID to reply to (keeps thread)",
                    "default": "",
                },
            },
            "required": ["to", "subject", "body"],
        },
    ),
    Tool(
        name="google_gmail_labels",
        description="List all Gmail labels (folders).",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ── Google Calendar ──
    Tool(
        name="google_calendar_list",
        description="List all available Google calendars.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="google_calendar_events",
        description="List upcoming calendar events. Supports time range and text search.",
        inputSchema={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "primary"},
                "time_min": {
                    "type": "string",
                    "description": "Start time ISO 8601 (default: now). E.g. '2026-04-01T00:00:00+03:00'",
                },
                "time_max": {
                    "type": "string",
                    "description": "End time ISO 8601. E.g. '2026-04-30T23:59:59+03:00'",
                },
                "max_results": {"type": "integer", "default": 10},
                "query": {"type": "string", "description": "Text search in events", "default": ""},
            },
        },
    ),
    Tool(
        name="google_calendar_create_event",
        description="Create a new calendar event.",
        inputSchema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start time ISO 8601 (e.g. '2026-04-05T10:00:00')"},
                "end": {"type": "string", "description": "End time ISO 8601 (e.g. '2026-04-05T11:00:00')"},
                "calendar_id": {"type": "string", "default": "primary"},
                "description": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "attendees": {
                    "type": "array", "items": {"type": "string"},
                    "description": "List of attendee emails",
                },
                "timezone": {"type": "string", "default": "Europe/Sofia"},
            },
            "required": ["summary", "start", "end"],
        },
    ),
    Tool(
        name="google_calendar_update_event",
        description="Update an existing calendar event. Only provided fields are changed.",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
                "summary": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "start": {"type": "string", "description": "New start time ISO 8601"},
                "end": {"type": "string", "description": "New end time ISO 8601"},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "timezone": {"type": "string", "default": "Europe/Sofia"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="google_calendar_delete_event",
        description="Delete a calendar event.",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
    ),
    # ── Telegram ──
    Tool(
        name="telegram_configure",
        description="Set Telegram API credentials (api_id and api_hash from my.telegram.org).",
        inputSchema={
            "type": "object",
            "properties": {
                "api_id": {"type": "string", "description": "API ID from my.telegram.org"},
                "api_hash": {"type": "string", "description": "API Hash from my.telegram.org"},
            },
            "required": ["api_id", "api_hash"],
        },
    ),
    Tool(
        name="telegram_auth",
        description=(
            "Authenticate with Telegram. Two-step process: "
            "1) Call with phone → code is sent to Telegram. "
            "2) Call with phone + code → authenticated. "
            "If 2FA enabled, provide password too."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Phone number with country code (e.g. +359886100204)"},
                "code": {"type": "string", "description": "Verification code from Telegram (step 2)", "default": ""},
                "password": {"type": "string", "description": "2FA password if enabled", "default": ""},
            },
            "required": ["phone"],
        },
    ),
    Tool(
        name="telegram_auth_status",
        description="Check Telegram authentication status.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="telegram_get_dialogs",
        description="List recent Telegram chats (users, groups, channels).",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="telegram_search_contacts",
        description="Search Telegram contacts by name or username.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (name or username)"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="telegram_get_messages",
        description="Read messages from a Telegram chat. Chat can be @username, phone, or numeric ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat": {"type": "string", "description": "Chat identifier (@username, +phone, or numeric ID)"},
                "limit": {"type": "integer", "default": 10},
                "search": {"type": "string", "description": "Search text in messages", "default": ""},
            },
            "required": ["chat"],
        },
    ),
    Tool(
        name="telegram_send_message",
        description="Send a Telegram message. Chat can be @username, phone, or numeric ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat": {"type": "string", "description": "Chat identifier (@username, +phone, or numeric ID)"},
                "message": {"type": "string", "description": "Message text"},
                "reply_to": {"type": "integer", "description": "Message ID to reply to", "default": 0},
            },
            "required": ["chat", "message"],
        },
    ),
    # ── Connection Manager GUI ──
    Tool(
        name="open_connection_manager",
        description=(
            "Open the Connection Manager GUI (desktop app). "
            "Launches GTK4 version on Linux, Qt6 on Windows/macOS. "
            "Use this when the user wants to visually manage connections, "
            "configure Portainer/GitHub, manage SSH keys, or see active sessions."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    # ── SSH Remote Execution ──
    Tool(
        name="ssh_execute",
        description=(
            "Execute a command on a remote server via SSH. "
            "Uses SSH config from connections.json (connection's ssh section). "
            "Provide either a connection alias (to use saved SSH config) or explicit host/user."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute remotely"},
                "connection": {"type": "string", "description": "Connection alias (uses its SSH config)", "default": ""},
                "host": {"type": "string", "description": "SSH host (if not using connection alias)", "default": ""},
                "user": {"type": "string", "description": "SSH user (if not using connection alias)", "default": ""},
                "port": {"type": "integer", "description": "SSH port", "default": 22},
                "timeout": {"type": "integer", "description": "Command timeout in seconds", "default": 30},
                "forward_agent": {"type": "boolean", "description": "Forward SSH agent to remote (for GitHub auth without storing keys)", "default": False},
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="git_remote",
        description=(
            "Run git commands on a remote server via SSH. "
            "Shortcut for common git operations (pull, status, log, branch, diff) "
            "on remote repositories. Provide the connection alias and repo path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "description": "Connection alias (uses its SSH config)"},
                "repo_path": {"type": "string", "description": "Path to git repo on remote server (e.g. /opt/odoo/odoo-19.0)"},
                "operation": {
                    "type": "string",
                    "description": "Git operation to perform",
                    "enum": ["status", "pull", "log", "branch", "diff", "remote", "fetch", "stash", "custom"],
                },
                "args": {"type": "string", "description": "Additional git arguments (e.g. '--oneline -10' for log, branch name for checkout)", "default": ""},
                "custom_command": {"type": "string", "description": "Full git command (only when operation=custom)", "default": ""},
                "forward_agent": {"type": "boolean", "description": "Forward SSH agent for GitHub auth (auto-enabled for pull/fetch/clone)", "default": True},
            },
            "required": ["connection", "repo_path", "operation"],
        },
    ),
    Tool(
        name="github_api",
        description=(
            "Call GitHub REST API directly. Uses the GitHub token from local_profile.json. "
            "For operations not covered by the GitHub MCP server, or when it's not running."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "API endpoint (e.g. /user/repos, /repos/owner/repo/issues)"},
                "method": {"type": "string", "description": "HTTP method", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"], "default": "GET"},
                "body": {"type": "object", "description": "Request body (for POST/PATCH/PUT)", "default": {}},
                "params": {"type": "object", "description": "Query parameters", "default": {}},
            },
            "required": ["endpoint"],
        },
    ),
    # ── User identity & per-user connections ──
    Tool(
        name="identify",
        description=(
            "Identify yourself to load your personal settings and connections. "
            "Call this at the start of a session. Returns your saved connections."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Your name (e.g. 'Rosen', 'Ivan')"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="who_am_i",
        description="Show current user identity and active connection.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="user_connection_add",
        description=(
            "Add/update a personal Odoo connection (saved per-user). "
            "Supports Odoo, SSH, and Portainer settings."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Connection name"},
                "url": {"type": "string", "description": "Odoo URL"},
                "db": {"type": "string", "description": "Database name"},
                "user": {"type": "string", "description": "Odoo username"},
                "api_key": {"type": "string", "description": "API key", "default": ""},
                "ssh_host": {"type": "string", "default": ""},
                "ssh_user": {"type": "string", "default": ""},
                "ssh_port": {"type": "integer", "default": 22},
                "portainer_url": {"type": "string", "default": ""},
                "portainer_token": {"type": "string", "default": ""},
                "web_login": {"type": "string", "default": "", "description": "Web session login (user/email)"},
                "web_password": {"type": "string", "default": "", "description": "Web session password"},
            },
            "required": ["alias", "url", "db", "user"],
        },
    ),
    Tool(
        name="user_connection_list",
        description="List your personal connections.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="user_connection_activate",
        description="Activate one of your personal connections as the working connection.",
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Connection name to activate"},
            },
            "required": ["alias"],
        },
    ),
    Tool(
        name="user_connection_delete",
        description="Delete a personal connection.",
        inputSchema={
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Connection name to delete"},
            },
            "required": ["alias"],
        },
    ),
    # ── Memory storage ──
    Tool(
        name="memory_list",
        description=(
            "List memory files. Shows personal files (for current user) and shared files. "
            "Use scope='personal', 'shared', or 'all' (default)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["all", "personal", "shared"],
                    "default": "all",
                    "description": "Which memories to list",
                },
            },
        },
    ),
    Tool(
        name="memory_read",
        description="Read a memory file by filename. Searches personal first, then shared.",
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "File name (e.g. 'module_l10n_bg.md')"},
                "scope": {
                    "type": "string",
                    "enum": ["personal", "shared"],
                    "default": "",
                    "description": "Force scope (default: search personal first, then shared)",
                },
            },
            "required": ["filename"],
        },
    ),
    Tool(
        name="memory_write",
        description=(
            "Write/update a memory file. Saves to personal storage by default. "
            "Content should be markdown with YAML frontmatter (name, description, type)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "File name (e.g. 'module_overview.md')"},
                "content": {"type": "string", "description": "Full file content (markdown with frontmatter)"},
                "scope": {
                    "type": "string",
                    "enum": ["personal", "shared"],
                    "default": "personal",
                    "description": "Where to save: personal (default) or shared",
                },
            },
            "required": ["filename", "content"],
        },
    ),
    Tool(
        name="memory_delete",
        description="Delete a memory file from personal or shared storage.",
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "File name to delete"},
                "scope": {
                    "type": "string",
                    "enum": ["personal", "shared"],
                    "default": "personal",
                },
            },
            "required": ["filename"],
        },
    ),
    Tool(
        name="memory_share",
        description=(
            "Share personal memory file(s) — copies to shared storage "
            "so colleagues can pull. Use filename='*' to share ALL personal files."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Personal file to share, or '*' for all"},
            },
            "required": ["filename"],
        },
    ),
    Tool(
        name="memory_pull",
        description=(
            "Pull shared memory file(s) into your personal storage. "
            "Use filename='*' to pull ALL shared files at once. "
            "If a file already exists locally, it gets overwritten with the shared version."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Shared file to pull, or '*' for all"},
            },
            "required": ["filename"],
        },
    ),
    # ── Proxy to internal MCP sub-services ──
    Tool(
        name="proxy_call",
        description=(
            "Forward a tool call to an internal MCP sub-service. "
            "Available services: 'portainer' (Docker/K8s management, 38 tools), "
            "'github' (repos/issues/PRs, 20 tools), "
            "'teams' (MS Teams messaging, 6 tools). "
            "Use proxy_discover first to see available tools on each service."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["portainer", "github", "teams"],
                    "description": "Target MCP service",
                },
                "tool": {"type": "string", "description": "Tool name on the target service"},
                "arguments": {
                    "type": "object",
                    "description": "Tool arguments (as JSON object)",
                    "default": {},
                },
            },
            "required": ["service", "tool"],
        },
    ),
    Tool(
        name="proxy_discover",
        description=(
            "List available tools on an internal MCP sub-service. "
            "Call this first to see what tools are available on portainer/github/teams."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["portainer", "github", "teams"],
                    "description": "Target MCP service",
                },
            },
            "required": ["service"],
        },
    ),
    Tool(
        name="proxy_refresh",
        description=(
            "Re-discover tools from all internal MCP sub-services. "
            "Use after starting a new sub-service or if tools are missing."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),

    # ── AI Tokenizer (Qdrant + Ollama, l10n_bg_claude_terminal v1.23+) ──
    Tool(
        name="ai_tokenize_record",
        description=(
            "Tokenize a single Odoo record: build composite document, embed via "
            "configured provider, upsert to Qdrant. Synchronous — returns final state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name (e.g. 'res.partner')"},
                "id": {"type": "integer", "description": "Record id"},
                "view_type": {"type": "string", "enum": ["form", "list", "kanban"], "default": "form"},
            },
            "required": ["model", "id"],
        },
    ),
    Tool(
        name="ai_tokenize_collection",
        description=(
            "Tokenize ALL records of a model via the registry entry. Heavy operation — "
            "for large models prefer the nightly cron. Returns count of processed records."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Model name"},
                "view_type": {"type": "string", "enum": ["form", "list", "kanban"], "default": "form"},
            },
            "required": ["model"],
        },
    ),
    Tool(
        name="ai_search_similar",
        description=(
            "Semantic search across tokenized Odoo records. Embeds the query with the "
            "configured provider, searches the per-DB Qdrant collection. Filterable by "
            "model, view_type, company. Returns ranked hits with score + snippet."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "query": {"type": "string", "description": "Natural-language query"},
                "model": {"type": "string", "description": "Restrict to one model (optional)", "default": ""},
                "view_type": {"type": "string", "default": ""},
                "company_id": {"type": "integer", "description": "Restrict to one company (optional)", "default": 0},
                "limit": {"type": "integer", "default": 10},
                "score_threshold": {"type": "number", "default": 0.0},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="ai_list_documents",
        description=(
            "List ai.composite.document records (tokenized records tracked by Odoo). "
            "Useful for monitoring tokenization progress / errors."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "model": {"type": "string", "description": "Filter by model_name", "default": ""},
                "state": {
                    "type": "string",
                    "enum": ["", "draft", "tokenized", "indexed", "stale", "error"],
                    "default": "",
                },
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="ai_collection_info",
        description=(
            "Return info about the per-database Qdrant collection: vector size, "
            "distance metric, point count, indexed-vectors count, plus Odoo-side "
            "indexed-document count for cross-check."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
            },
        },
    ),
    # ── AI Invoice Extraction + Billing Ledger ────────
    Tool(
        name="ai_invoice_extract",
        description=(
            "Extract structured invoice data from an Odoo attachment via Anthropic "
            "Vision. Auto-routes to haiku/sonnet/opus based on pages+size. Writes "
            "extract_prefill_data back to account.move (if write_back). Logs usage "
            "to the billing ledger with token counts + cost in millicents."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "attachment_id": {"type": "integer", "description": "ir.attachment id"},
                "move_id": {
                    "type": "integer",
                    "description": "account.move id (target for write-back). 0 = skip write-back.",
                    "default": 0,
                },
                "source": {
                    "type": "string",
                    "enum": ["upload", "gmail", "terminal", "api"],
                    "default": "upload",
                },
                "source_message_id": {
                    "type": "string",
                    "description": "Gmail Message-ID for dedup. Only used when source='gmail'.",
                    "default": "",
                },
                "model_override": {
                    "type": "string",
                    "description": "Force a specific model, bypassing routing. Leave empty for auto.",
                    "default": "",
                },
                "write_back": {
                    "type": "boolean",
                    "description": "Write extracted data to account.move.extract_prefill_data.",
                    "default": True,
                },
                "tenant_tier": {
                    "type": "string",
                    "enum": ["starter", "business", "professional", "enterprise"],
                    "default": "business",
                },
            },
            "required": ["attachment_id"],
        },
    ),
    Tool(
        name="ai_usage_log_query",
        description=(
            "Query the AI usage billing ledger. Filter by tenant, state, source, "
            "date range. Returns detailed rows for audit + monthly reconciliation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "tenant_code": {
                    "type": "string",
                    "description": "Tenant slug. Derived from connection if omitted.",
                    "default": "",
                },
                "state": {
                    "type": "string",
                    "enum": ["", "success", "error", "cached", "skipped"],
                    "default": "",
                },
                "source": {
                    "type": "string",
                    "enum": ["", "upload", "gmail", "terminal", "api"],
                    "default": "",
                },
                "date_from": {
                    "type": "string",
                    "description": "ISO-8601 UTC (inclusive).",
                    "default": "",
                },
                "date_to": {
                    "type": "string",
                    "description": "ISO-8601 UTC (inclusive).",
                    "default": "",
                },
                "billed_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    Tool(
        name="ai_usage_log_stats",
        description=(
            "Dashboard KPIs for the billing ledger: totals, per-model breakdown, "
            "30-day timeseries, recent errors, derived metrics (cache hit rate, "
            "avg cost per billed doc, monthly €)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "tenant_code": {
                    "type": "string",
                    "description": "Tenant slug. Derived from connection if omitted.",
                    "default": "",
                },
                "period": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year", "all"],
                    "default": "month",
                },
            },
        },
    ),
    Tool(
        name="ai_usage_log_export",
        description=(
            "Export billing ledger rows as CSV for a tenant + date range. "
            "Used for month-end invoice generation and audit trails."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "tenant_code": {
                    "type": "string",
                    "description": "Tenant slug. Derived from connection if omitted.",
                    "default": "",
                },
                "date_from": {
                    "type": "string",
                    "description": "ISO-8601 UTC (inclusive).",
                    "default": "",
                },
                "date_to": {
                    "type": "string",
                    "description": "ISO-8601 UTC (inclusive).",
                    "default": "",
                },
            },
        },
    ),
    # ── AI Invoice Pipeline Engine (pluggable steps) ──────
    Tool(
        name="ai_invoice_stack_inspect",
        description=(
            "Cross-layer snapshot for a single account.move: Odoo state + "
            "attachments + extraction history + skill status + decided next_step "
            "with blockers and hints. Read-only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "move_id": {"type": "integer"},
                "tenant_code": {"type": "string", "default": ""},
            },
            "required": ["move_id"],
        },
    ),
    Tool(
        name="ai_invoice_scan_pending",
        description=(
            "List vendor-bill drafts that have an attachment and no successful "
            "extraction yet. Oldest first (FIFO). Feeds batch processing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "tenant_code": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="ai_invoice_pipeline_summary",
        description=(
            "Dashboard aggregation: counts of vendor bill moves by next_step "
            "across the last 60 days. Use for header KPI cards."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "tenant_code": {"type": "string", "default": ""},
            },
        },
    ),
    Tool(
        name="ai_invoice_pipeline_run",
        description=(
            "Execute the full registered step pipeline for one move+attachment. "
            "Steps: probe_move → guard_already_extracted → extract_vision → "
            "log_usage → write_back_move → invoke_posting_skill + any loaded plugins. "
            "Returns step-by-step audit trail."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "tenant_code": {"type": "string", "default": ""},
                "tenant_tier": {
                    "type": "string",
                    "enum": ["starter", "business", "professional", "enterprise"],
                    "default": "business",
                },
                "move_id": {"type": "integer"},
                "attachment_id": {
                    "type": "integer",
                    "description": "Specific attachment id. 0 = auto-pick from move.",
                    "default": 0,
                },
                "source": {
                    "type": "string",
                    "enum": ["upload", "gmail", "terminal", "api", "force"],
                    "default": "upload",
                },
                "source_message_id": {"type": "string", "default": ""},
            },
            "required": ["move_id"],
        },
    ),
    Tool(
        name="ai_invoice_pipeline_steps",
        description=(
            "List currently registered pipeline steps with sequence, description, "
            "and plugin vs built-in provenance. Useful for debugging which steps "
            "will run against a move."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="ai_invoice_plugins_reload",
        description=(
            "Reload plugin steps from the plugins directory (default "
            "/data/plugins/ai_invoice). Called after uploading a new plugin "
            "without restarting the server."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plugins_dir": {
                    "type": "string",
                    "description": "Override default plugins directory.",
                    "default": "",
                },
            },
        },
    ),
    # ── Odoo-driven pipeline executor (ai.pipeline.step) ──
    Tool(
        name="ai_pipeline_run",
        description=(
            "Execute an Odoo-defined pipeline (ai.pipeline.step records for the "
            "given pipeline name, ordered by sequence). Respects skill_id + "
            "trigger_domain + on_error. MCP-native steps (model starts with "
            "'mcp') are dispatched to the local registry; other steps are "
            "invoked via RPC on the configured Odoo model.method. Writes back "
            "last_run_state/message per step."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "pipeline": {
                    "type": "string",
                    "description": "Pipeline name as defined in Odoo (tokenize, post, refresh, ...).",
                },
                "source_model": {
                    "type": "string",
                    "description": "Odoo model of the triggering record (e.g. account.move).",
                },
                "source_id": {"type": "integer"},
                "tenant_code": {"type": "string", "default": ""},
                "tenant_tier": {
                    "type": "string",
                    "enum": ["starter", "business", "professional", "enterprise"],
                    "default": "business",
                },
                "extra_ctx": {
                    "type": "object",
                    "description": "Additional key/value pairs merged into the runtime context.",
                    "default": {},
                },
                "update_step_stats": {
                    "type": "boolean",
                    "description": "Write last_run_state/message back to ai.pipeline.step.",
                    "default": True,
                },
            },
            "required": ["pipeline", "source_model", "source_id"],
        },
    ),
    Tool(
        name="ai_pipeline_steps_list",
        description=(
            "List ai.pipeline.step records in Odoo for a given pipeline name. "
            "Shows sequence, model.method, skill_id, on_error, last run state — "
            "mirrors the Odoo Settings view."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection": {"type": "string", "default": "default"},
                "pipeline": {
                    "type": "string",
                    "description": "Pipeline name (tokenize/post/refresh/...)",
                    "default": "tokenize",
                },
                "include_inactive": {"type": "boolean", "default": False},
            },
        },
    ),
]


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    base = [t for t in TOOLS if t.name != "odoo_disconnect"] if SINGLE_CONNECTION else list(TOOLS)
    # Append dynamically discovered proxy tools
    if PROXY_TOOL_DEFS:
        base.extend(PROXY_TOOL_DEFS)
    return base


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # ── Handle proxied tools directly in async context ──
        if name in PROXY_TOOLS:
            info = PROXY_TOOLS[name]
            result = await _async_proxy_call(info["service"], info["tool"], arguments)
        elif name == "proxy_call":
            result = await _async_proxy_call(
                arguments["service"], arguments["tool"], arguments.get("arguments", {}))
        elif name == "proxy_refresh":
            await asyncio.get_event_loop().run_in_executor(None, _discover_proxy_tools, None)
            result = {
                "status": "refreshed",
                "proxied_tools": len(PROXY_TOOLS),
                "services": {svc: sum(1 for v in PROXY_TOOLS.values() if v["service"] == svc)
                             for svc in _get_proxy_services()},
            }
        else:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _execute_tool, name, arguments
            )
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if len(text) > 100_000:
            text = text[:100_000] + "\n... (truncated, use limit/fields to narrow)"
        return [TextContent(type="text", text=text)]
    except Exception as e:
        logger.error(f"Tool {name} error: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


def _open_connection_manager() -> dict:
    """Launch the Connection Manager GUI or return command for host execution."""
    import platform
    import subprocess

    in_docker = Path("/.dockerenv").exists()
    system = platform.system()

    # Paths to try (host-side)
    gtk4_paths = [
        "~/Проекти/odoo/odoo-18.0/claude.ai/tools/odoo_connect.py",
        "~/odoo-claude-mcp/tools/odoo_connect.py",
    ]
    qt6_paths = [
        "~/Проекти/odoo/odoo-mcp/tools/odoo_connect_qt.py",
        "~/odoo-claude-mcp/tools/odoo_connect_qt.py",
    ]

    if in_docker:
        # Running inside Docker — can't launch GUI directly.
        # Return the command for Claude Code to execute on the host via Bash tool.
        if system == "Linux":
            return {
                "action": "run_on_host",
                "message": "MCP server is inside Docker. Please run this command on the host:",
                "commands": {
                    "linux_gtk4": f"python3 {gtk4_paths[0]}",
                    "linux_qt6": f"python3 {qt6_paths[0]}",
                    "windows": f"python {qt6_paths[1]}",
                    "macos": f"python3 {qt6_paths[1]}",
                },
                "recommended": f"python3 {gtk4_paths[0]}",
            }
        return {
            "action": "run_on_host",
            "message": "MCP server is inside Docker. Run on the host:",
            "recommended": f"python3 {qt6_paths[0]}",
        }

    # Running on host — launch directly
    script = None
    gui_type = None

    if system == "Linux":
        for p in gtk4_paths:
            expanded = Path(p).expanduser()
            if expanded.exists():
                script, gui_type = str(expanded), "GTK4"
                break
        if not script:
            for p in qt6_paths:
                expanded = Path(p).expanduser()
                if expanded.exists():
                    script, gui_type = str(expanded), "Qt6"
                    break
    else:
        for p in qt6_paths:
            expanded = Path(p).expanduser()
            if expanded.exists():
                script, gui_type = str(expanded), "Qt6"
                break

    if not script:
        return {"error": "Connection Manager GUI not found",
                "hint": "Install: pip install PySide6 (Qt6) or PyGObject (GTK4)"}

    env = os.environ.copy()
    if system == "Linux" and gui_type == "Qt6":
        env["QT_QPA_PLATFORMTHEME"] = "gtk3"

    try:
        subprocess.Popen(
            [sys.executable, script],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"status": "launched", "gui": gui_type, "script": script}
    except Exception as e:
        return {"error": f"Failed to launch: {e}", "script": script}


def _ssh_execute(host: str, user: str, command: str, port: int = 22,
                  key_filename: str = None, timeout: int = 30,
                  forward_agent: bool = False) -> dict:
    """Execute a command on remote server via SSH using paramiko.

    If forward_agent=True, the local SSH agent is forwarded to the remote
    server so it can authenticate with third parties (e.g. GitHub) using
    your local keys — without storing any keys on the remote server.
    """
    import paramiko
    from paramiko.agent import AgentRequestHandler

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        connect_kwargs = {"hostname": host, "username": user, "port": port, "timeout": 10}
        if key_filename:
            connect_kwargs["key_filename"] = key_filename
        else:
            connect_kwargs["allow_agent"] = True
            connect_kwargs["look_for_keys"] = True
            home_ssh = Path.home() / ".ssh"
            key_candidates = [home_ssh / k for k in ("id_ed25519", "id_ecdsa", "id_rsa")
                              if (home_ssh / k).exists()]
            if key_candidates:
                connect_kwargs["key_filename"] = [str(k) for k in key_candidates]

        client.connect(**connect_kwargs)

        # Open channel with agent forwarding
        transport = client.get_transport()
        channel = transport.open_session()

        if forward_agent:
            AgentRequestHandler(channel)

        channel.exec_command(command)

        # Read output with timeout
        channel.settimeout(timeout)
        stdout_data = b""
        stderr_data = b""
        while True:
            if channel.recv_ready():
                stdout_data += channel.recv(65536)
            if channel.recv_stderr_ready():
                stderr_data += channel.recv_stderr(65536)
            if channel.exit_status_ready():
                # Drain remaining
                while channel.recv_ready():
                    stdout_data += channel.recv(65536)
                while channel.recv_stderr_ready():
                    stderr_data += channel.recv_stderr(65536)
                break

        exit_code = channel.recv_exit_status()
        out = stdout_data.decode("utf-8", errors="replace")
        err = stderr_data.decode("utf-8", errors="replace")
        return {
            "status": "ok",
            "exit_code": exit_code,
            "stdout": out.strip(),
            "stderr": err.strip(),
            "host": f"{user}@{host}:{port}",
            "command": command,
            "agent_forwarded": forward_agent,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "host": f"{user}@{host}:{port}"}
    finally:
        client.close()


def _get_ssh_config(connection_alias: str) -> dict:
    """Get SSH config from connections.json for a given alias."""
    candidates = [
        Path("/config/connections.json"),      # Docker mount from claude.ai
        CONNECTIONS_FILE,                       # /data/connections.json
        Path.home() / "Проекти" / "odoo" / "odoo-18.0" / "claude.ai" / ".odoo_connections" / "connections.json",
    ]
    for conns_file in candidates:
        try:
            if conns_file.exists():
                with open(conns_file, "r") as f:
                    conns = json.load(f)
                if isinstance(conns, dict) and connection_alias in conns:
                    ssh = conns[connection_alias].get("ssh", {})
                    if ssh:
                        return ssh
        except Exception:
            continue
    return {}


def _github_api_call(endpoint: str, method: str = "GET",
                     body: dict = None, params: dict = None) -> dict:
    """Call GitHub REST API using token from local_profile.json."""
    import urllib.request
    import urllib.parse

    # Find token
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if not token:
        for profile_path in [
            Path.home() / "Проекти" / "odoo" / "odoo-18.0" / "claude.ai" / ".odoo_connections" / "local_profile.json",
            CONNECTIONS_FILE.parent / "local_profile.json",
        ]:
            if profile_path.exists():
                try:
                    with open(profile_path) as f:
                        token = json.load(f).get("github_token", "")
                    if token:
                        break
                except Exception:
                    pass

    if not token:
        return {"error": "No GitHub token found. Set in local_profile.json or GITHUB_PERSONAL_ACCESS_TOKEN env."}

    url = f"https://api.github.com{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    data = None
    if body and method in ("POST", "PATCH", "PUT"):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = resp.read().decode()
            if response_body:
                return json.loads(response_body)
            return {"status": resp.status}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        try:
            return {"error": json.loads(err_body), "status": e.code}
        except Exception:
            return {"error": err_body, "status": e.code}


def _execute_tool(name: str, args: dict) -> Any:
    m = _mgr()

    # ── Connection management ──
    if name == "odoo_connect":
        if SINGLE_CONNECTION:
            # Only allow replacing the "default" connection
            args["alias"] = "default"
        conn = m.add(
            alias=args.get("alias", "default"),
            url=args["url"],
            db=args["db"],
            username=args["username"],
            password=args.get("password", ""),
            api_key=args.get("api_key", ""),
            protocol=args.get("protocol", "xmlrpc"),
            verify_ssl=bool(args.get("verify_ssl", True)),
        )
        # Test authentication — also triggers first-time cert fetch when
        # verify_ssl=False.
        uid = conn.authenticate()
        return {"status": "connected", "uid": uid, **conn.to_dict()}

    elif name == "odoo_cert_info":
        alias = args.get("alias", "default")
        conn = m.get(alias)
        if conn.verify_ssl:
            return {
                "error": "Connection uses standard CA verification "
                         "(verify_ssl=True). No pinned cert.",
                "verify_ssl": True,
            }
        path = conn._cached_cert_path()
        if not path.exists():
            return {
                "error": f"No pinned cert yet for alias '{alias}'. "
                         "Call authenticate first or use odoo_cert_refresh.",
                "pinned_cert_path": str(path),
            }
        try:
            import ssl as _ssl
            pem = path.read_text()
            import subprocess as _sub
            out = _sub.run(
                ["openssl", "x509", "-noout",
                 "-subject", "-issuer", "-dates", "-fingerprint", "-sha256"],
                input=pem, capture_output=True, text=True, timeout=10,
            )
            info = dict(
                (line.split("=", 1) if "=" in line else (line, ""))
                for line in out.stdout.strip().splitlines()
            )
        except Exception as e:  # noqa: BLE001
            info = {"parse_error": str(e)[:200]}
        return {
            "alias": alias,
            "pinned_cert_path": str(path),
            "pinned_cert_size": path.stat().st_size,
            "pem_head": pem.splitlines()[0] if pem else "",
            "info": info,
        }

    elif name == "odoo_cert_refresh":
        alias = args.get("alias", "default")
        conn = m.get(alias)
        if conn.verify_ssl:
            return {"error": "Connection has verify_ssl=True — no cert to refresh."}
        conn._ssl_ctx_cache = None  # clear cached context
        try:
            path = conn._fetch_and_cache_cert()
            return {
                "status": "refreshed", "alias": alias,
                "pinned_cert_path": str(path),
                "size": path.stat().st_size,
            }
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:400]}

    elif name == "odoo_disconnect":
        if SINGLE_CONNECTION:
            return {"error": "Single-connection mode: cannot remove connections."}
        alias = args.get("alias", "default")
        ok = m.remove(alias)
        return {"status": "removed" if ok else "not_found", "alias": alias}

    elif name == "odoo_connections":
        return {"connections": m.list_all()}

    elif name == "identify":
        # Unified-auth priority: if HTTP middleware validated the caller,
        # use that identity (cannot be spoofed via args["name"]). Fall
        # back to args["name"] only for stdio/local contexts where no
        # HTTP validation is available.
        caller = _odoo_caller_ctx.get()
        if caller:
            user_name = caller["mcp_user"]
            safe_name = user_name  # already a safe dir name from registry
            preferred_alias = caller.get("alias")
        else:
            user_name = args.get("name") or ""
            if not user_name:
                return {
                    "status": "error",
                    "error": "name is required (or supply unified-auth "
                             "Authorization + X-Odoo-* headers).",
                }
            safe_name = _sanitize_name(user_name)
            preferred_alias = None

        is_new = not os.path.isdir(os.path.join(DATA_DIR, "users", safe_name))

        # Only write to session_users when we're NOT relying on unified-auth
        # ContextVar. When caller is set, ContextVar is authoritative and
        # session_users would just be stale noise for later calls.
        if not caller:
            session_key = _get_mcp_session_key()
            _session_users[session_key] = user_name
            _session_users["current"] = user_name  # backward compat

        conns = _load_user_connections(user_name)
        active = _load_user_active(user_name)
        alias_to_activate = preferred_alias or active.get("alias")
        if alias_to_activate and alias_to_activate in conns:
            c = conns[alias_to_activate]
            try:
                conn = m.add(
                    alias="default",
                    url=c["url"], db=c["db"],
                    username=c["user"],
                    api_key=c.get("api_key", ""),
                    password=c.get("password", ""),
                    protocol=c.get("protocol", "xmlrpc"),
                )
                conn.authenticate()
                logger.info(f"User {user_name}: auto-activated connection '{alias_to_activate}'")
            except Exception as e:
                logger.warning(f"User {user_name}: auto-activate '{alias_to_activate}' failed: {e}")

        result = {
            "status": "new_profile" if is_new else "identified",
            "user": user_name,
            "profile": safe_name,
            "connections": list(conns.keys()),
            "active": alias_to_activate,
            "validated": bool(caller),
        }
        if is_new:
            result["hint"] = (
                f"Profile '{safe_name}' is new. "
                f"Data will be saved on first write."
            )
        return result

    elif name == "who_am_i":
        user = _get_current_user(args)
        if not user:
            return {"status": "not_identified", "hint": "Call identify(name) first"}
        active = _load_user_active(user)
        conns = _load_user_connections(user)
        return {
            "user": user,
            "connections": list(conns.keys()),
            "active": active.get("alias", None),
            "active_details": active,
        }

    elif name == "user_connection_add":
        user = _get_current_user(args)
        if not user:
            return {"error": "Call identify(name) first"}
        conns = _load_user_connections(user)
        alias = args["alias"]
        conn_data = {
            "url": args["url"],
            "db": args["db"],
            "user": args["user"],
            "api_key": args.get("api_key", ""),
        }
        if args.get("ssh_host"):
            conn_data["ssh"] = {
                "host": args["ssh_host"],
                "user": args.get("ssh_user", "root"),
                "port": args.get("ssh_port", 22),
            }
        if args.get("portainer_url"):
            conn_data["portainer"] = {
                "url": args["portainer_url"],
                "token": args.get("portainer_token", ""),
            }
        if args.get("web_login"):
            conn_data["web"] = {
                "login": args["web_login"],
                "password": args.get("web_password", ""),
            }
        conns[alias] = conn_data
        _save_user_connections(user, conns)
        return {"status": "saved", "alias": alias, "user": user}

    elif name == "user_connection_list":
        user = _get_current_user(args)
        if not user:
            return {"error": "Call identify(name) first"}
        conns = _load_user_connections(user)
        active = _load_user_active(user)
        result = []
        for alias, c in conns.items():
            entry = {"alias": alias, "url": c.get("url", ""), "db": c.get("db", "")}
            if alias == active.get("alias"):
                entry["active"] = True
            if c.get("ssh"):
                entry["ssh"] = f"{c['ssh'].get('user', '')}@{c['ssh'].get('host', '')}"
            if c.get("portainer"):
                entry["portainer"] = c["portainer"].get("url", "")
            result.append(entry)
        return {"user": user, "connections": result}

    elif name == "user_connection_activate":
        user = _get_current_user(args)
        if not user:
            return {"error": "Call identify(name) first"}
        alias = args["alias"]
        conns = _load_user_connections(user)
        if alias not in conns:
            return {"error": f"Connection '{alias}' not found. Use user_connection_list to see available."}
        c = conns[alias]
        _save_user_active(user, {"alias": alias, **c})
        # Also activate in MCP server
        try:
            conn = m.add(
                alias="default",
                url=c["url"], db=c["db"],
                username=c["user"],
                api_key=c.get("api_key", ""),
                password=c.get("password", ""),
                protocol=c.get("protocol", "xmlrpc"),
            )
            uid = conn.authenticate()
            return {"status": "activated", "alias": alias, "uid": uid, "url": c["url"]}
        except Exception as e:
            _save_user_active(user, {"alias": alias, **c})
            return {"status": "activated_no_auth", "alias": alias, "error": str(e),
                    "hint": "Connection saved but auth failed. Check credentials."}

    elif name == "user_connection_delete":
        user = _get_current_user(args)
        if not user:
            return {"error": "Call identify(name) first"}
        alias = args["alias"]
        conns = _load_user_connections(user)
        if alias not in conns:
            return {"error": f"Connection '{alias}' not found"}
        del conns[alias]
        _save_user_connections(user, conns)
        # Clear active if this was active
        active = _load_user_active(user)
        if active.get("alias") == alias:
            _save_user_active(user, {})
        return {"status": "deleted", "alias": alias}

    # ── Memory storage ──
    elif name == "memory_list":
        scope = args.get("scope", "all")
        result = {}
        user = _get_current_user(args)
        if scope in ("all", "personal"):
            if user:
                result["personal"] = _memory_list_files(_memory_user_dir(user))
            else:
                result["personal"] = []
                result["hint"] = "Call identify(name) to see personal files"
        if scope in ("all", "shared"):
            result["shared"] = _memory_list_files(_memory_shared_dir())
        if scope in ("all", "licensed"):
            # Licensed memories come from purchased memory packs; resolve
            # the tenant via the caller's Odoo context (db_name becomes
            # tenant_code for billing lookups).
            caller = _odoo_caller_ctx.get() if "_odoo_caller_ctx" in globals() else None
            tenant_code = (args.get("tenant_code") or "").strip()
            if not tenant_code and caller and caller.get("db"):
                tenant_code = caller["db"]
            if tenant_code:
                result["licensed"] = _memory_list_files(
                    _memory_licensed_dir(tenant_code),
                )
                result["licensed_tenant"] = tenant_code
            else:
                result["licensed"] = []
                result["licensed_hint"] = (
                    "Pass tenant_code or call through an authenticated "
                    "Odoo connection to see licensed memories."
                )
        return result

    elif name == "memory_read":
        filename = args["filename"]
        if not filename.endswith(".md"):
            filename += ".md"
        # Sanitize
        filename = os.path.basename(filename)
        scope = args.get("scope", "")
        user = _get_current_user(args)

        # Licensed scope uses tenant_code; resolve from auth ctx if absent.
        def _resolve_tenant():
            tc = (args.get("tenant_code") or "").strip()
            if tc:
                return tc
            caller = _odoo_caller_ctx.get() if "_odoo_caller_ctx" in globals() else None
            return (caller or {}).get("db") if caller else None

        fpath = None
        found_scope = None
        if scope == "personal" and user:
            fpath = os.path.join(_memory_user_dir(user), filename)
            found_scope = "personal"
        elif scope == "shared":
            fpath = os.path.join(_memory_shared_dir(), filename)
            found_scope = "shared"
        elif scope == "licensed":
            tc = _resolve_tenant()
            if tc:
                fpath = os.path.join(_memory_licensed_dir(tc), filename)
                found_scope = "licensed"
        else:
            # Search personal → licensed → shared
            if user:
                p = os.path.join(_memory_user_dir(user), filename)
                if os.path.isfile(p):
                    fpath = p
                    found_scope = "personal"
            if not fpath:
                tc = _resolve_tenant()
                if tc:
                    p = os.path.join(_memory_licensed_dir(tc), filename)
                    if os.path.isfile(p):
                        fpath = p
                        found_scope = "licensed"
            if not fpath:
                p = os.path.join(_memory_shared_dir(), filename)
                if os.path.isfile(p):
                    fpath = p
                    found_scope = "shared"

        if not fpath or not os.path.isfile(fpath):
            return {"error": f"File '{filename}' not found"}
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        return {"filename": filename, "scope": found_scope, "content": content}

    elif name == "memory_write":
        filename = args["filename"]
        if not filename.endswith(".md"):
            filename += ".md"
        filename = os.path.basename(filename)
        content = args["content"]
        scope = args.get("scope", "personal")

        if scope == "shared":
            directory = _memory_shared_dir(create=True)
        else:
            user = _get_current_user(args)
            if not user:
                return {"error": "Call identify(name) first to write personal files"}
            directory = _memory_user_dir(user, create=True)

        fpath = os.path.join(directory, filename)
        existed = os.path.isfile(fpath)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        return {
            "status": "updated" if existed else "created",
            "filename": filename,
            "scope": scope,
            "size": len(content),
        }

    elif name == "memory_delete":
        filename = os.path.basename(args["filename"])
        scope = args.get("scope", "personal")
        if scope == "shared":
            fpath = os.path.join(_memory_shared_dir(), filename)
        else:
            user = _get_current_user(args)
            if not user:
                return {"error": "Call identify(name) first"}
            fpath = os.path.join(_memory_user_dir(user), filename)
        if not os.path.isfile(fpath):
            return {"error": f"File '{filename}' not found in {scope}"}
        os.remove(fpath)
        return {"status": "deleted", "filename": filename, "scope": scope}

    elif name == "memory_share":
        user = _get_current_user(args)
        if not user:
            return {"error": "Call identify(name) first"}
        import shutil
        filename = args["filename"].strip()
        user_dir = _memory_user_dir(user)
        shared_dir = _memory_shared_dir(create=True)

        if filename == "*":
            files = [f for f in os.listdir(user_dir) if f.endswith(".md")]
            if not files:
                return {"status": "empty", "message": "No personal files to share"}
            shared = []
            for f in files:
                shutil.copy2(os.path.join(user_dir, f), os.path.join(shared_dir, f))
                shared.append(f)
            return {
                "status": "shared_all",
                "shared": shared,
                "total": len(shared),
                "shared_by": user,
            }

        filename = os.path.basename(filename)
        src = os.path.join(user_dir, filename)
        if not os.path.isfile(src):
            return {"error": f"Personal file '{filename}' not found"}
        dst = os.path.join(shared_dir, filename)
        shutil.copy2(src, dst)
        return {
            "status": "shared",
            "filename": filename,
            "shared_by": user,
        }

    elif name == "memory_pull":
        user = _get_current_user(args)
        if not user:
            return {"error": "Call identify(name) first"}
        import shutil
        filename = args["filename"].strip()
        user_dir = _memory_user_dir(user, create=True)
        shared_dir = _memory_shared_dir()

        if filename == "*":
            # Pull all shared files
            files = [f for f in os.listdir(shared_dir) if f.endswith(".md")]
            if not files:
                return {"status": "empty", "message": "No shared files to pull"}
            pulled = []
            updated = []
            for f in files:
                dst = os.path.join(user_dir, f)
                existed = os.path.isfile(dst)
                shutil.copy2(os.path.join(shared_dir, f), dst)
                (updated if existed else pulled).append(f)
            return {
                "status": "pulled_all",
                "pulled": pulled,
                "updated": updated,
                "total": len(files),
                "user": user,
            }

        filename = os.path.basename(filename)
        src = os.path.join(shared_dir, filename)
        if not os.path.isfile(src):
            return {"error": f"Shared file '{filename}' not found"}
        dst = os.path.join(user_dir, filename)
        existed = os.path.isfile(dst)
        shutil.copy2(src, dst)
        return {
            "status": "updated" if existed else "pulled",
            "filename": filename,
            "user": user,
        }

    # ── Proxy to internal sub-services ──
    elif name == "proxy_call":
        return _proxy_call(args["service"], args["tool"], args.get("arguments", {}))

    elif name == "proxy_discover":
        tools = _proxy_list_tools(args["service"])
        return {"service": args["service"], "tools": tools, "count": len(tools)}

    elif name == "proxy_refresh":
        _discover_proxy_tools()
        return {
            "status": "refreshed",
            "proxied_tools": len(PROXY_TOOLS),
            "services": {svc: sum(1 for v in PROXY_TOOLS.values() if v["service"] == svc)
                         for svc in _get_proxy_services()},
        }

    # ── Dynamically proxied tools (prefix__toolname) ──
    elif name in PROXY_TOOLS:
        info = PROXY_TOOLS[name]
        return _proxy_call(info["service"], info["tool"], args)

    elif name == "open_connection_manager":
        return _open_connection_manager()

    elif name == "ssh_execute":
        command = args["command"]
        timeout = args.get("timeout", 30)
        conn_alias = args.get("connection", "")
        host = args.get("host", "")
        user = args.get("user", "")
        port = args.get("port", 22)
        key_file = None

        if conn_alias:
            ssh_cfg = _get_ssh_config(conn_alias)
            if not ssh_cfg:
                return {"error": f"No SSH config for connection '{conn_alias}'"}
            host = ssh_cfg.get("host", host)
            user = ssh_cfg.get("user", user)
            port = ssh_cfg.get("port", port)
            if ssh_cfg.get("identity_file"):
                key_file = ssh_cfg["identity_file"]

        if not host or not user:
            return {"error": "SSH host and user required (provide connection alias or explicit host/user)"}

        fwd = args.get("forward_agent", False)
        logger.info(f"SSH exec: {user}@{host}:{port} agent={fwd} $ {command}")
        return _ssh_execute(host, user, command, port, key_file, timeout, forward_agent=fwd)

    elif name == "git_remote":
        conn_alias = args["connection"]
        repo_path = args["repo_path"]
        operation = args["operation"]
        extra_args = args.get("args", "")

        ssh_cfg = _get_ssh_config(conn_alias)
        if not ssh_cfg:
            return {"error": f"No SSH config for connection '{conn_alias}'"}

        host = ssh_cfg.get("host", "")
        user = ssh_cfg.get("user", "")
        port = ssh_cfg.get("port", 22)
        key_file = ssh_cfg.get("identity_file") or None

        if not host or not user:
            return {"error": f"SSH config incomplete for '{conn_alias}'"}

        git_commands = {
            "status": "git status",
            "pull": "git pull",
            "log": f"git log --oneline {extra_args or '-20'}",
            "branch": f"git branch {extra_args}",
            "diff": f"git diff {extra_args}",
            "remote": "git remote -v",
            "fetch": "git fetch --all",
            "stash": f"git stash {extra_args}",
            "custom": extra_args or args.get("custom_command", ""),
        }

        git_cmd = git_commands.get(operation, "")
        if not git_cmd:
            return {"error": f"Unknown operation: {operation}"}

        # Auto-enable agent forwarding for operations that need GitHub auth
        fwd = args.get("forward_agent", True)
        needs_remote = operation in ("pull", "fetch", "custom")
        use_fwd = fwd and needs_remote

        full_cmd = f"cd {repo_path} && {git_cmd}"
        logger.info(f"Git remote: {user}@{host} agent={use_fwd} $ {full_cmd}")
        return _ssh_execute(host, user, full_cmd, port, key_file, 60, forward_agent=use_fwd)

    elif name == "github_api":
        return _github_api_call(
            endpoint=args["endpoint"],
            method=args.get("method", "GET"),
            body=args.get("body"),
            params=args.get("params"),
        )

    # ── All other tools need a connection ──
    conn = _conn(args)

    if name == "odoo_version":
        common = xmlrpc.client.ServerProxy(
            f"{conn.url}/xmlrpc/2/common", allow_none=True
        )
        return common.version()

    elif name == "odoo_list_models":
        domain: list = []
        pattern = args.get("pattern", "")
        if pattern:
            domain = [["model", "ilike", f"%{pattern}%"]]
        return conn.execute_kw(
            "ir.model", "search_read",
            [domain],
            {"fields": ["model", "name", "state", "transient"], "limit": args.get("limit", 100)},
        )

    elif name == "odoo_fields_get":
        return conn.execute_kw(
            args["model"], "fields_get",
            [],
            {"attributes": args.get("attributes", ["string", "type", "required", "readonly", "relation"])},
        )

    elif name == "odoo_search":
        kw: dict[str, Any] = {"limit": args.get("limit", 80), "offset": args.get("offset", 0)}
        if args.get("order"):
            kw["order"] = args["order"]
        return conn.execute_kw(args["model"], "search", [args.get("domain", [])], kw)

    elif name == "odoo_read":
        kw = {}
        fields = args.get("fields", [])
        if fields:
            kw["fields"] = fields
        return conn.execute_kw(args["model"], "read", [args["ids"]], kw)

    elif name == "odoo_search_read":
        kw: dict[str, Any] = {"limit": args.get("limit", 80), "offset": args.get("offset", 0)}
        fields = args.get("fields", [])
        if fields:
            kw["fields"] = fields
        if args.get("order"):
            kw["order"] = args["order"]
        return conn.execute_kw(args["model"], "search_read", [args.get("domain", [])], kw)

    elif name == "odoo_search_count":
        return conn.execute_kw(args["model"], "search_count", [args.get("domain", [])])

    elif name == "odoo_create":
        vals = args["values"]
        if isinstance(vals, dict):
            vals = [vals]
        ids = []
        for v in vals:
            result = conn.execute_kw(args["model"], "create", [v])
            ids.append(result)
        # Live refresh: notify list views that a new row appeared
        _notify_live_refresh(conn, "list", args["model"], ids, vals[0] if vals else {})
        return {"created_ids": ids}

    elif name == "odoo_write":
        result = conn.execute_kw(args["model"], "write", [args["ids"], args["values"]])
        # Live refresh: notify form views to update specific fields
        _notify_live_refresh(conn, "field", args["model"], args["ids"], args["values"])
        return {"success": result, "ids": args["ids"]}

    elif name == "odoo_unlink":
        result = conn.execute_kw(args["model"], "unlink", [args["ids"]])
        return {"success": result, "ids": args["ids"]}

    elif name == "odoo_execute":
        return conn.execute_kw(
            args["model"],
            args["method"],
            args.get("args", []),
            args.get("kwargs", {}),
        )

    elif name == "odoo_message_post":
        model = args["model"]
        res_id = int(args["res_id"])
        body_md = args["body"]
        msg_type = args.get("message_type", "note")
        body_html = _md_to_html(body_md)
        kwargs = {
            "body": body_html,
            "body_is_html": True,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_note" if msg_type == "note" else "mail.mt_comment",
        }
        if args.get("subject"):
            kwargs["subject"] = args["subject"]
        if args.get("partner_ids"):
            kwargs["partner_ids"] = args["partner_ids"]
        if args.get("attachment_ids"):
            kwargs["attachment_ids"] = args["attachment_ids"]
        message_id = conn.execute_kw(model, "message_post", [[res_id]], kwargs)
        _notify_live_refresh(conn, "field", model, [res_id])
        return {"message_id": message_id, "model": model, "res_id": res_id, "type": msg_type}

    elif name == "odoo_attachment_upload":
        model = args["model"]
        res_id = int(args["res_id"])
        filename = args["filename"]
        content_b64 = args["content_base64"]
        vals = {
            "name": filename,
            "datas": content_b64,
            "res_model": model,
            "res_id": res_id,
            "type": "binary",
        }
        if args.get("mimetype"):
            vals["mimetype"] = args["mimetype"]
        att_id = conn.execute_kw("ir.attachment", "create", [vals])
        return {"attachment_id": att_id, "filename": filename, "model": model, "res_id": res_id}

    elif name == "odoo_attachment_download":
        att_id = int(args["attachment_id"])
        save_path = args.get("save_path", "")
        records = conn.execute_kw(
            "ir.attachment", "read", [[att_id]],
            {"fields": ["name", "datas", "mimetype", "file_size"]},
        )
        if not records:
            return {"error": f"Attachment {att_id} not found"}
        rec = records[0]
        content_b64 = rec.get("datas") or ""
        result = {
            "attachment_id": att_id,
            "filename": rec.get("name", ""),
            "mimetype": rec.get("mimetype", ""),
            "size": rec.get("file_size", 0),
        }
        if save_path:
            import base64
            data = base64.b64decode(content_b64)
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(data)
            result["saved_to"] = save_path
            result["bytes_written"] = len(data)
        else:
            result["content_base64"] = content_b64
        return result

    elif name == "odoo_module_info":
        module_name = args["module"]
        result = {"module": module_name, "odoo": None, "filesystem": []}

        # ── 1. Odoo RPC: module state ──
        try:
            records = conn.execute_kw(
                "ir.module.module", "search_read",
                [[["name", "=", module_name]]],
                {"fields": [
                    "name", "state", "shortdesc", "summary", "author",
                    "website", "license", "installed_version", "latest_version",
                    "category_id", "application", "auto_install",
                    "icon", "to_buy",
                ]},
            )
            if records:
                rec = records[0]
                # Get dependencies from Odoo
                dep_ids = conn.execute_kw(
                    "ir.module.module.dependency", "search_read",
                    [[["module_id", "=", rec["id"]]]],
                    {"fields": ["name", "depend_id", "auto_install_required"]},
                )
                rec["depends"] = [d["name"] for d in dep_ids]
                # Get dependents (who depends on this module)
                rev_dep_ids = conn.execute_kw(
                    "ir.module.module.dependency", "search_read",
                    [[["name", "=", module_name]]],
                    {"fields": ["module_id"]},
                )
                rec["dependents"] = [
                    d["module_id"][1] for d in rev_dep_ids if d.get("module_id")
                ]
                result["odoo"] = rec
        except Exception as e:
            result["odoo_error"] = str(e)

        # ── 2. Filesystem: scan /repos for the module ──
        repos_dir = os.environ.get("REPOS_DIR", "/repos")
        if os.path.isdir(repos_dir):
            for instance in sorted(os.listdir(repos_dir)):
                instance_path = os.path.join(repos_dir, instance)
                if not os.path.isdir(instance_path):
                    continue

                # Check OCA repos
                oca_base = os.path.join(instance_path, "oca")
                if os.path.isdir(oca_base):
                    for repo_name in sorted(os.listdir(oca_base)):
                        mod_path = os.path.join(oca_base, repo_name, module_name)
                        manifest = os.path.join(mod_path, "__manifest__.py")
                        if os.path.isfile(manifest):
                            result["filesystem"].append(
                                _parse_fs_module(mod_path, manifest, "oca", repo_name, instance)
                            )

                # Check EE
                for ee_sub in ["ee/enterprise", "ee"]:
                    mod_path = os.path.join(instance_path, ee_sub, module_name)
                    manifest = os.path.join(mod_path, "__manifest__.py")
                    if os.path.isfile(manifest):
                        result["filesystem"].append(
                            _parse_fs_module(mod_path, manifest, "ee", "enterprise", instance)
                        )
                        break

                # Check custom
                custom_base = os.path.join(instance_path, "custom")
                if os.path.isdir(custom_base):
                    for repo_name in sorted(os.listdir(custom_base)):
                        mod_path = os.path.join(custom_base, repo_name, module_name)
                        manifest = os.path.join(mod_path, "__manifest__.py")
                        if os.path.isfile(manifest):
                            result["filesystem"].append(
                                _parse_fs_module(mod_path, manifest, "custom", repo_name, instance)
                            )

        # ── 3. Summary ──
        sources = [f["source"] for f in result["filesystem"]]
        result["found_in"] = sorted(set(sources)) if sources else ["odoo_core_only" if result.get("odoo") else "not_found"]
        if result.get("odoo"):
            result["installed"] = result["odoo"].get("state") == "installed"
        else:
            result["installed"] = False

        return result

    # ── Web Session handlers ──

    elif name == "odoo_web_login":
        alias = args.get("connection", "default")
        url = args.get("url", "")
        db = args.get("db", "")
        login = args.get("login", "")
        password = args.get("password", "")

        # Fall back to RPC connection config + saved web credentials
        if not url or not db or not login or not password:
            try:
                rpc_conn = _conn(args)
                url = url or rpc_conn.url
                db = db or rpc_conn.db
            except Exception:
                pass

            # Check saved user connection for web credentials
            if not login or not password:
                current_user = _get_current_user(args)
                if current_user:
                    conns = _load_user_connections(current_user)
                    conn_cfg = conns.get(alias, {})
                    web_cfg = conn_cfg.get("web", {})
                    login = login or web_cfg.get("login", conn_cfg.get("user", ""))
                    password = password or web_cfg.get("password", conn_cfg.get("api_key", ""))

        if not url or not db:
            return {"error": "URL and database required. Provide explicitly or configure RPC connection first."}
        if not login or not password:
            return {"error": "Login and password required. Provide explicitly or save with user_connection_add(web_login=, web_password=)."}

        ws = OdooWebSession(url, db, login, password)
        try:
            info = ws.authenticate()
            _web_sessions[alias] = ws
            return {
                "status": "authenticated",
                "uid": ws.uid,
                "name": info.get("name", ""),
                "session_id": ws.session_id[:12] + "...",
                "server_version": info.get("server_version", ""),
                "connection": alias,
            }
        except Exception as e:
            return {"error": str(e)}

    elif name == "odoo_web_call":
        ws = _get_web_session(args)
        return ws.call_kw(args["model"], args["method"], args.get("args", []), args.get("kwargs", {}))

    elif name == "odoo_web_read":
        ws = _get_web_session(args)
        return ws.web_read(
            args["model"], args.get("domain", []), args["fields"],
            args.get("limit", 80), args.get("offset", 0), args.get("order", ""),
        )

    elif name == "odoo_web_export":
        ws = _get_web_session(args)
        return ws.export_data(
            args["model"], args.get("domain", []), args["fields"],
            args.get("import_compat", False),
        )

    elif name == "odoo_web_report":
        ws = _get_web_session(args)
        import base64 as b64
        pdf_bytes = ws.get_report_pdf(args["report_name"], args["ids"])
        save_path = args.get("save_path", "")
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(pdf_bytes)
            return {"status": "saved", "path": save_path, "size": len(pdf_bytes)}
        return {"content_base64": b64.b64encode(pdf_bytes).decode(), "size": len(pdf_bytes)}

    elif name == "odoo_web_request":
        ws = _get_web_session(args)
        return ws.raw_request(
            args["path"], args.get("method", "GET"),
            args.get("data", {}), args.get("params", {}),
        )

    elif name == "odoo_web_logout":
        alias = args.get("connection", "default")
        ws = _web_sessions.pop(alias, None)
        if ws:
            ws.destroy()
            return {"status": "logged_out", "connection": alias}
        return {"status": "no_session", "connection": alias}

    # ── Public Access handlers ──

    elif name == "public_access_export_xlsx" or name == "public_access_export_csv":
        ws = _get_web_session(args)
        import base64 as b64, re as _re
        model = args["model"]
        fields = args["fields"]
        domain = args.get("domain", [])

        # Get record IDs
        ids = ws.call_kw(model, "search", [domain])
        if not ids:
            return {"status": "empty", "records": 0}

        # Get CSRF token from /odoo page
        csrf_page = ws.session.get(f"{ws.url}/odoo", verify=False, timeout=15)
        csrf_match = _re.search(r'csrf_token[^"\']*["\x27]([a-f0-9]+o\d+)["\x27]', csrf_page.text)
        csrf = csrf_match.group(1) if csrf_match else ""

        # Build export params with label (required by Odoo export controller)
        fmt = "xlsx" if name.endswith("xlsx") else "csv"
        export_fields = [{"name": f, "label": f} for f in fields]

        import json as _json
        export_data = _json.dumps({
            "model": model,
            "fields": export_fields,
            "ids": ids,
            "domain": domain,
            "import_compat": args.get("import_compat", False),
        })

        resp = ws.session.post(
            f"{ws.url}/web/export/{fmt}",
            data={"data": export_data, "csrf_token": csrf},
            verify=False, timeout=120,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "body": resp.text[:500]}
        content = resp.content
        save_path = args.get("save_path", "")
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(content)
            return {"status": "saved", "path": save_path, "size": len(content), "format": fmt, "records": len(ids)}
        return {"content_base64": b64.b64encode(content).decode(), "size": len(content), "format": fmt, "records": len(ids)}

    elif name == "public_access_report_pdf":
        ws = _get_web_session(args)
        import base64 as b64
        ids_str = ",".join(str(i) for i in args["ids"])
        resp = ws.session.get(
            f"{ws.url}/report/pdf/{args['report_name']}/{ids_str}",
            verify=False, timeout=120,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "body": resp.text[:500]}
        save_path = args.get("save_path", "")
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return {"status": "saved", "path": save_path, "size": len(resp.content)}
        return {"content_base64": b64.b64encode(resp.content).decode(), "size": len(resp.content)}

    elif name == "public_access_report_html":
        ws = _get_web_session(args)
        ids_str = ",".join(str(i) for i in args["ids"])
        resp = ws.session.get(
            f"{ws.url}/report/html/{args['report_name']}/{ids_str}",
            verify=False, timeout=120,
        )
        return {"status": resp.status_code, "html": resp.text[:10000], "size": len(resp.content)}

    elif name == "public_access_download":
        ws = _get_web_session(args)
        import base64 as b64
        resp = ws.session.get(
            f"{ws.url}/web/content/{args['content_id']}",
            verify=False, timeout=120,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        ct = resp.headers.get("Content-Type", "")
        cd = resp.headers.get("Content-Disposition", "")
        filename = ""
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('"').strip("'")
        save_path = args.get("save_path", "")
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return {"status": "saved", "path": save_path, "size": len(resp.content), "content_type": ct, "filename": filename}
        return {"content_base64": b64.b64encode(resp.content).decode(), "size": len(resp.content), "content_type": ct, "filename": filename}

    elif name == "public_access_image":
        ws = _get_web_session(args)
        import base64 as b64
        field = args.get("field", "image_1920")
        resp = ws.session.get(
            f"{ws.url}/web/image/{args['model']}/{args['record_id']}/{field}",
            verify=False, timeout=60,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        ct = resp.headers.get("Content-Type", "")
        save_path = args.get("save_path", "")
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return {"status": "saved", "path": save_path, "size": len(resp.content), "content_type": ct}
        return {"content_base64": b64.b64encode(resp.content).decode(), "size": len(resp.content), "content_type": ct}

    elif name == "public_access_barcode":
        ws = _get_web_session(args)
        import base64 as b64
        btype = args.get("barcode_type", "Code128")
        value = args["value"]
        w = args.get("width", 600)
        h = args.get("height", 100)
        resp = ws.session.get(
            f"{ws.url}/report/barcode/{btype}/{value}",
            params={"width": w, "height": h},
            verify=False, timeout=30,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        save_path = args.get("save_path", "")
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return {"status": "saved", "path": save_path, "size": len(resp.content)}
        return {"content_base64": b64.b64encode(resp.content).decode(), "size": len(resp.content), "type": btype}

    elif name in ("public_access_portal_home", "public_access_portal_invoices",
                   "public_access_portal_orders", "public_access_portal_purchases",
                   "public_access_portal_tickets"):
        ws = _get_web_session(args)
        path_map = {
            "public_access_portal_home": "/my/home",
            "public_access_portal_invoices": "/my/invoices",
            "public_access_portal_orders": "/my/orders",
            "public_access_portal_purchases": "/my/purchase",
            "public_access_portal_tickets": "/my/tickets",
        }
        path = path_map[name]
        page = args.get("page", 1)
        params = {"page": page} if page > 1 else {}
        resp = ws.session.get(f"{ws.url}{path}", params=params, verify=False, timeout=30)
        # Extract useful text from HTML
        body = resp.text
        # Simple extraction: strip scripts/styles, keep text
        import re
        body = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL)
        body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL)
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        return {"status": resp.status_code, "path": path, "text": body[:8000], "size": len(resp.content)}

    elif name == "public_access_report_xlsx":
        ws = _get_web_session(args)
        import base64 as b64
        ids_str = ",".join(str(i) for i in args["ids"])
        resp = ws.session.get(
            f"{ws.url}/report/xlsx/{args['report_name']}/{ids_str}",
            verify=False, timeout=120,
        )
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "body": resp.text[:500]}
        save_path = args.get("save_path", "")
        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(resp.content)
            return {"status": "saved", "path": save_path, "size": len(resp.content)}
        return {"content_base64": b64.b64encode(resp.content).decode(), "size": len(resp.content)}

    elif name == "public_access_shop":
        ws = _get_web_session(args)
        params = {}
        if args.get("page", 1) > 1:
            params["page"] = args["page"]
        if args.get("search"):
            params["search"] = args["search"]
        resp = ws.session.get(f"{ws.url}/shop", params=params, verify=False, timeout=30)
        import re
        body = resp.text
        body = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL)
        body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL)
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        return {"status": resp.status_code, "text": body[:8000], "size": len(resp.content)}

    elif name == "public_access_sitemap":
        ws = _get_web_session(args)
        resp = ws.session.get(f"{ws.url}/sitemap.xml", verify=False, timeout=30)
        return {"status": resp.status_code, "content": resp.text[:10000], "size": len(resp.content)}

    elif name == "odoo_report":
        report_obj = xmlrpc.client.ServerProxy(
            f"{conn.url}/xmlrpc/2/report", allow_none=True
        )
        uid = conn.authenticate()
        result = report_obj.render_report(
            conn.db, uid, conn.auth_token,
            args["report_name"], args["ids"],
        )
        return {
            "format": result.get("format", "pdf"),
            "content_base64": result.get("result", ""),
            "state": result.get("state", False),
        }

    # ── View Refresh ──
    elif name == "odoo_refresh":
        payload = {
            "model": args.get("model", ""),
            "res_id": args.get("res_id", 0),
        }
        conn.execute_kw(
            "res.users", "notify_claude_refresh",
            [payload],
        )
        return {"status": "refresh_sent", **payload}

    # ── Fiscal Position Configuration ──
    elif name == "odoo_fp_list":
        domain: list = []
        if args.get("company_id"):
            domain.append(["company_id", "=", args["company_id"]])
        if args.get("country_id"):
            domain.append(["country_id", "=", args["country_id"]])
        if args.get("name"):
            domain.append(["name", "ilike", args["name"]])
        positions = conn.execute_kw(
            "account.fiscal.position", "search_read",
            [domain],
            {
                "fields": ["id", "name", "auto_apply", "country_id",
                           "country_group_id", "company_id", "tax_action_map_ids"],
                "limit": args.get("limit", 50),
            },
        )
        for pos in positions:
            pos["action_count"] = len(pos.get("tax_action_map_ids", []))
        return positions

    elif name == "odoo_fp_details":
        pos_id = args["position_id"]
        positions = conn.execute_kw(
            "account.fiscal.position", "read",
            [[pos_id]],
            {"fields": ["id", "name", "auto_apply", "country_id",
                         "country_group_id", "company_id", "tax_ids",
                         "account_ids", "tax_action_map_ids"]},
        )
        if not positions:
            return {"error": f"Fiscal position {pos_id} not found"}
        position = positions[0]
        action_ids = position.get("tax_action_map_ids", [])
        actions = []
        if action_ids:
            actions = conn.execute_kw(
                "account.fiscal.position.tax.action", "search_read",
                [[["id", "in", action_ids]]],
                {"fields": FP_ACTION_FIELDS},
            )
        position["tax_action_map_entries"] = actions
        return position

    elif name == "odoo_fp_configure":
        action_id = args.get("action_id")
        vals = {}
        for field in FP_ACTION_FIELDS:
            if field == "id":
                continue
            if field in args and args[field] is not None:
                vals[field] = args[field]
        # Sync legacy field
        if "l10n_bg_document_type" in vals:
            vals["l10n_bg_doc_type"] = vals["l10n_bg_document_type"]

        if action_id:
            conn.execute_kw(
                "account.fiscal.position.tax.action", "write",
                [[action_id], vals],
            )
            result = conn.execute_kw(
                "account.fiscal.position.tax.action", "read",
                [[action_id]],
                {"fields": FP_ACTION_FIELDS},
            )
            return {"status": "updated", "action": result[0] if result else {}}
        else:
            if "position_id" not in vals:
                return {"error": "position_id is required when creating a new action"}
            if "l10n_bg_narration" not in vals:
                return {"error": "l10n_bg_narration is required"}
            new_id = conn.execute_kw(
                "account.fiscal.position.tax.action", "create",
                [vals],
            )
            result = conn.execute_kw(
                "account.fiscal.position.tax.action", "read",
                [[new_id]],
                {"fields": FP_ACTION_FIELDS},
            )
            return {"status": "created", "action": result[0] if result else {"id": new_id}}

    elif name == "odoo_fp_remove_action":
        action_id = args["action_id"]
        result = conn.execute_kw(
            "account.fiscal.position.tax.action", "unlink",
            [[action_id]],
        )
        return {"status": "removed", "action_id": action_id, "success": result}

    elif name == "odoo_fp_types":
        if args.get("live"):
            fields_data = conn.execute_kw(
                "account.fiscal.position.tax.action", "fields_get",
                [],
                {"attributes": ["string", "type", "selection"]},
            )
            types = {}
            for fname in ["move_type", "l10n_bg_move_type", "l10n_bg_type_vat",
                          "l10n_bg_document_type", "l10n_bg_doc_type"]:
                if fname in fields_data and "selection" in fields_data[fname]:
                    types[fname] = fields_data[fname]["selection"]
            return {"source": "live", "types": types}
        return {
            "source": "cached",
            "types": {
                "move_type": FP_MOVE_TYPES,
                "l10n_bg_move_type": FP_BG_MOVE_TYPES,
                "l10n_bg_document_type": FP_DOC_TYPES,
                "l10n_bg_type_vat": FP_TYPE_VAT,
            },
            "note": "Use live=true to fetch current values from Odoo",
        }

    # ── AI Tokenizer (l10n_bg_claude_terminal v1.23+) ──
    elif name == "ai_tokenize_record":
        result = conn.execute_kw(
            "ai.view.registry", "tokenize_record",
            [args["model"], int(args["id"]), args.get("view_type", "form")],
        )
        return result

    elif name == "ai_tokenize_collection":
        # Find or create registry entry, then run action_tokenize_all on it.
        ir_model = conn.execute_kw("ir.model", "search_read",
            [[["model", "=", args["model"]]]],
            {"fields": ["id"], "limit": 1})
        if not ir_model:
            return {"error": f"Unknown model: {args['model']}"}
        view_type = args.get("view_type", "form")
        registry_ids = conn.execute_kw("ai.view.registry", "search",
            [[["model_id", "=", ir_model[0]["id"]], ["view_type", "=", view_type]]],
            {"limit": 1})
        if not registry_ids:
            registry_id = conn.execute_kw("ai.view.registry", "create",
                [{"model_id": ir_model[0]["id"], "view_type": view_type, "active": True}])
            conn.execute_kw("ai.view.registry", "action_parse_arch", [[registry_id]])
            registry_ids = [registry_id]
        # Activate if archived
        conn.execute_kw("ai.view.registry", "write",
            [registry_ids, {"active": True}])
        conn.execute_kw("ai.view.registry", "action_tokenize_all", [registry_ids])
        # Return doc count for this registry
        count = conn.execute_kw("ai.composite.document", "search_count",
            [[["registry_id", "=", registry_ids[0]], ["state", "=", "indexed"]]])
        return {"ok": True, "registry_id": registry_ids[0], "indexed_count": count}

    elif name == "ai_search_similar":
        kwargs = {
            "query": args["query"],
            "limit": int(args.get("limit", 10)),
            "score_threshold": float(args.get("score_threshold", 0.0)),
        }
        if args.get("model"):
            kwargs["model_name"] = args["model"]
        if args.get("view_type"):
            kwargs["view_type"] = args["view_type"]
        if args.get("company_id"):
            kwargs["company_id"] = int(args["company_id"])
        return conn.execute_kw(
            "ai.composite.document", "search_similar",
            [], kwargs,
        )

    elif name == "ai_list_documents":
        domain = []
        if args.get("model"):
            domain.append(["model_name", "=", args["model"]])
        if args.get("state"):
            domain.append(["state", "=", args["state"]])
        records = conn.execute_kw(
            "ai.composite.document", "search_read",
            [domain],
            {
                "fields": ["display_name", "model_name", "res_id", "view_type",
                           "state", "token_count", "qdrant_point_id",
                           "source_write_date", "error_message"],
                "limit": int(args.get("limit", 50)),
                "order": "write_date desc",
            },
        )
        return {"count": len(records), "records": records}

    elif name == "ai_collection_info":
        return conn.execute_kw("ai.composite.document", "collection_stats", [])

    # ── AI Invoice Extraction + Billing Ledger ────────
    elif name == "ai_invoice_extract":
        import base64 as _b64
        import json as _json

        attachment_id = int(args["attachment_id"])
        move_id = int(args.get("move_id") or 0)
        source = args.get("source", "upload")
        source_msg_id = args.get("source_message_id") or None
        model_override = args.get("model_override") or None
        write_back = bool(args.get("write_back", True))
        tenant_tier = args.get("tenant_tier", "business")

        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        api_key, base_url = _ai_tenant_credentials(tenant_code)
        if not api_key:
            return {"error": (
                "No Anthropic API key configured. Set ANTHROPIC_API_KEY "
                "or ANTHROPIC_API_KEY_<TENANT> env var."
            )}

        # 1. Read attachment from Odoo (datas = base64 string)
        records = conn.execute_kw(
            "ir.attachment", "read", [[attachment_id]],
            {"fields": ["name", "datas", "mimetype", "file_size"]},
        )
        if not records:
            return {"error": f"Attachment {attachment_id} not found"}
        att = records[0]
        file_bytes = _b64.b64decode(att.get("datas") or "")
        mimetype = att.get("mimetype") or "application/pdf"
        if not file_bytes:
            return {"error": f"Attachment {attachment_id} has no data"}

        # 2. Call vision extractor (no side effects other than HTTP)
        result = ai_vision_service.extract_invoice(
            file_bytes=file_bytes,
            mimetype=mimetype,
            api_key=api_key,
            base_url=base_url,
            tenant_tier=tenant_tier,
            model_override=model_override,
        )

        # 3. Write-back to Odoo (only on success and if requested)
        writeback_info: dict = {"attempted": False}
        if result.state == "success" and write_back and move_id:
            writeback_info = _ai_write_back_to_move(
                conn, move_id, result.extracted_data or {}, attachment_id,
            )

        # 4. Log usage (always — success, cached, or error)
        log_id = ai_usage_log.log_extraction(
            tenant_code=tenant_code,
            odoo_url=conn.url,
            odoo_db=conn.db,
            move_id=move_id or None,
            attachment_id=attachment_id,
            source=source,
            source_message_id=source_msg_id if source == "gmail" else None,
            extra={
                "attachment_name": att.get("name"),
                "attachment_size": att.get("file_size"),
                "mimetype": mimetype,
                "writeback": writeback_info,
            },
            **result.to_log_kwargs(),
        )

        return {
            "ok": result.state in ("success", "cached"),
            "state": result.state,
            "model": result.model,
            "pages": result.pages,
            "tokens": {
                "input": result.input_tokens,
                "output": result.output_tokens,
                "cache_read": result.cache_read_tokens,
                "cache_creation": result.cache_creation_tokens,
            },
            "cost": {
                "usd_millicents": result.cost_usd_millicents,
                "eur_millicents": result.cost_eur_millicents,
                "eur_display": f"€{result.cost_eur_millicents / 100_000:.4f}",
            },
            "duration_ms": result.duration_ms,
            "extracted_data": result.extracted_data,
            "writeback": writeback_info,
            "log_id": log_id,
            "error": result.error_message,
        }

    elif name == "ai_usage_log_query":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        rows = ai_usage_log.query(
            tenant_code=tenant_code,
            state=args.get("state") or None,
            source=args.get("source") or None,
            date_from=args.get("date_from") or None,
            date_to=args.get("date_to") or None,
            billed_only=bool(args.get("billed_only", False)),
            limit=int(args.get("limit", 100)),
            offset=int(args.get("offset", 0)),
        )
        return {"tenant_code": tenant_code, "count": len(rows), "rows": rows}

    elif name == "ai_usage_log_stats":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        return ai_usage_log.stats(tenant_code, args.get("period", "month"))

    elif name == "ai_usage_log_export":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        csv_data = ai_usage_log.export_csv(
            tenant_code=tenant_code,
            date_from=args.get("date_from") or None,
            date_to=args.get("date_to") or None,
        )
        return {
            "tenant_code": tenant_code,
            "rows": csv_data.count("\n") - 1 if csv_data else 0,
            "csv": csv_data,
        }

    # ── AI Invoice Pipeline Engine ────────────────────
    elif name == "ai_invoice_stack_inspect":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        stack = ai_invoice_engine.inspect_stack(
            conn=conn,
            move_id=int(args["move_id"]),
            tenant_code=tenant_code,
        )
        return stack.to_dict()

    elif name == "ai_invoice_scan_pending":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        pending = ai_invoice_engine.scan_pending(
            conn=conn,
            tenant_code=tenant_code,
            limit=int(args.get("limit", 50)),
        )
        return {
            "tenant_code": tenant_code,
            "count": len(pending),
            "pending": pending,
        }

    elif name == "ai_invoice_pipeline_summary":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        return ai_invoice_engine.pipeline_summary(
            conn=conn, tenant_code=tenant_code,
        )

    elif name == "ai_invoice_pipeline_run":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        tenant_tier = args.get("tenant_tier", "business")
        api_key, base_url = _ai_tenant_credentials(tenant_code)
        if not api_key:
            return {"error": (
                "No Anthropic API key configured. Set ANTHROPIC_API_KEY "
                "or ANTHROPIC_API_KEY_<TENANT> env var."
            )}
        ctx = ai_invoice_engine.PipelineContext(
            odoo_conn=conn,
            tenant_code=tenant_code,
            tenant_tier=tenant_tier,
            api_key=api_key,
            base_url=base_url,
            move_id=int(args["move_id"]),
            attachment_id=(int(args.get("attachment_id") or 0) or None),
            source=args.get("source", "upload"),
            source_message_id=args.get("source_message_id") or None,
        )
        run = ai_invoice_engine.run_pipeline(ctx)
        # Strip non-JSON-serializable vision_result before returning
        final = dict(run.final_data)
        vr = final.pop("vision_result", None)
        if vr:
            final["vision_summary"] = {
                "state": vr.state, "model": vr.model,
                "pages": vr.pages, "duration_ms": vr.duration_ms,
                "cost_eur_millicents": vr.cost_eur_millicents,
            }
        out = run.to_dict()
        out["final_data"] = final
        return out

    elif name == "ai_invoice_pipeline_steps":
        steps = ai_invoice_engine.registry.all()
        return {
            "count": len(steps),
            "steps": [
                {
                    "name": s.name,
                    "sequence": s.sequence,
                    "description": s.description,
                    "on_error": s.on_error,
                    "has_applies_when": s.applies_when is not None,
                }
                for s in steps
            ],
        }

    elif name == "ai_invoice_plugins_reload":
        plugins_dir = args.get("plugins_dir") or os.environ.get(
            "AI_INVOICE_PLUGINS_DIR", "/data/plugins/ai_invoice"
        )
        loaded = ai_invoice_engine.load_plugins(plugins_dir)
        return {
            "plugins_dir": plugins_dir,
            "loaded": loaded,
            "total_steps_registered": len(ai_invoice_engine.registry.names()),
        }

    # ── Odoo-driven pipeline executor ──────────────────
    elif name == "ai_pipeline_run":
        tenant_code = _ai_tenant_code(conn, args.get("tenant_code"))
        api_key, base_url = _ai_tenant_credentials(tenant_code)
        pipeline_run = ai_invoice_engine.run_odoo_pipeline(
            conn=conn,
            pipeline=args["pipeline"],
            source_model=args["source_model"],
            source_id=int(args["source_id"]),
            tenant_code=tenant_code,
            tenant_tier=args.get("tenant_tier", "business"),
            api_key=api_key,
            base_url=base_url,
            extra_ctx=args.get("extra_ctx") or {},
            update_step_stats=bool(args.get("update_step_stats", True)),
        )
        return pipeline_run.to_dict()

    elif name == "ai_pipeline_steps_list":
        domain = [["pipeline", "=", args.get("pipeline", "tokenize")]]
        if not args.get("include_inactive"):
            domain.append(["active", "=", True])
        rows = conn.execute_kw(
            "ai.pipeline.step", "search_read", [domain],
            {"fields": [
                "id", "name", "pipeline", "sequence",
                "model", "method", "skill_id", "trigger_domain",
                "on_error", "active", "module",
                "last_run_state", "last_run_message", "last_run_date",
            ], "order": "sequence, id"},
        )
        return {
            "pipeline": args.get("pipeline", "tokenize"),
            "count": len(rows),
            "steps": rows,
        }

    # ── Google Services ──
    elif name == "google_auth":
        if google_mgr is None:
            return {"error": "Google service not initialized"}
        return google_mgr.authenticate(args.get("credentials_file", ""))

    elif name == "google_auth_status":
        if google_mgr is None:
            return {"status": "not_initialized"}
        return {
            "status": "authenticated" if google_mgr.is_authenticated else "not_authenticated",
            "email": google_mgr._get_email() if google_mgr.is_authenticated else None,
        }

    elif name == "google_gmail_search":
        return google_mgr.gmail_search(
            query=args["query"],
            max_results=args.get("max_results", 10),
            label_ids=args.get("label_ids"),
        )

    elif name == "google_gmail_read":
        return google_mgr.gmail_read(args["message_id"])

    elif name == "google_gmail_send":
        return google_mgr.gmail_send(
            to=args["to"],
            subject=args["subject"],
            body=args["body"],
            cc=args.get("cc", ""),
            bcc=args.get("bcc", ""),
            html=args.get("html", False),
            reply_to_message_id=args.get("reply_to_message_id", ""),
        )

    elif name == "google_gmail_labels":
        return google_mgr.gmail_labels()

    elif name == "google_calendar_list":
        return google_mgr.calendar_list()

    elif name == "google_calendar_events":
        return google_mgr.calendar_events(
            calendar_id=args.get("calendar_id", "primary"),
            time_min=args.get("time_min", ""),
            time_max=args.get("time_max", ""),
            max_results=args.get("max_results", 10),
            query=args.get("query", ""),
        )

    elif name == "google_calendar_create_event":
        return google_mgr.calendar_create_event(
            summary=args["summary"],
            start=args["start"],
            end=args["end"],
            calendar_id=args.get("calendar_id", "primary"),
            description=args.get("description", ""),
            location=args.get("location", ""),
            attendees=args.get("attendees"),
            timezone_str=args.get("timezone", "Europe/Sofia"),
        )

    elif name == "google_calendar_update_event":
        event_id = args.pop("event_id")
        calendar_id = args.pop("calendar_id", "primary")
        return google_mgr.calendar_update_event(
            event_id=event_id, calendar_id=calendar_id, **args,
        )

    elif name == "google_calendar_delete_event":
        return google_mgr.calendar_delete_event(
            event_id=args["event_id"],
            calendar_id=args.get("calendar_id", "primary"),
        )

    # ── Telegram ──
    elif name == "telegram_configure":
        if telegram_mgr is None:
            return {"error": "Telegram service not initialized"}
        return telegram_mgr.configure(args["api_id"], args["api_hash"])

    elif name == "telegram_auth":
        if telegram_mgr is None:
            return {"error": "Telegram service not initialized"}
        code = args.get("code", "")
        if code:
            return telegram_mgr.auth_verify(
                phone=args["phone"], code=code,
                password=args.get("password", ""),
            )
        return telegram_mgr.auth_send_code(args["phone"])

    elif name == "telegram_auth_status":
        if telegram_mgr is None:
            return {"status": "not_initialized"}
        return telegram_mgr.auth_status()

    elif name == "telegram_get_dialogs":
        return telegram_mgr.get_dialogs(limit=args.get("limit", 20))

    elif name == "telegram_search_contacts":
        return telegram_mgr.search_contacts(args["query"])

    elif name == "telegram_get_messages":
        chat = args["chat"]
        if chat.lstrip("-").isdigit():
            chat = int(chat)
        return telegram_mgr.get_messages(
            chat=chat, limit=args.get("limit", 10),
            search=args.get("search", ""),
        )

    elif name == "telegram_send_message":
        chat = args["chat"]
        if chat.lstrip("-").isdigit():
            chat = int(chat)
        return telegram_mgr.send_message(
            chat=chat, message=args["message"],
            reply_to=args.get("reply_to", 0),
        )

    return {"error": f"Unknown tool: {name}"}


# ─── Starlette ASGI app ──────────────────────────────────────
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
import uvicorn


async def health_endpoint(request):
    m = _mgr()
    return JSONResponse({
        "status": "ok",
        "service": "odoo-rpc-mcp",
        "connections": len(m.connections) if m else 0,
        "aliases": list(m.connections.keys()) if m else [],
        "timestamp": datetime.now().isoformat(),
    })


def create_app():
    global manager, google_mgr, telegram_mgr, session_mgr

    manager = ConnectionManager(CONNECTIONS_FILE)
    logger.info(f"Loaded {len(manager.connections)} connection(s): {list(manager.connections.keys())}")

    session_mgr = SessionManager(SESSIONS_DB)
    stale = session_mgr.cleanup_stale()
    if stale:
        logger.info(f"Cleaned up {stale} stale session(s)")
    logger.info(f"Session manager ready at {SESSIONS_DB}")

    google_mgr = GoogleServiceManager()
    if google_mgr.is_authenticated:
        logger.info("Google services: authenticated")
    else:
        logger.info("Google services: not authenticated (call google_auth to connect)")

    telegram_mgr = TelegramServiceManager()
    if telegram_mgr.is_authenticated:
        logger.info("Telegram: authenticated")
    else:
        logger.info("Telegram: not authenticated (call telegram_configure + telegram_auth)")

    # --- AI Invoice Pipeline plugin discovery ---
    _plugins_dir = os.environ.get(
        "AI_INVOICE_PLUGINS_DIR", "/data/plugins/ai_invoice"
    )
    _loaded = ai_invoice_engine.load_plugins(_plugins_dir)
    logger.info(
        f"AI Invoice pipeline: {len(ai_invoice_engine.registry.names())} steps "
        f"registered ({len(_loaded)} plugin(s) from {_plugins_dir})"
    )

    # --- SSE transport (legacy, /sse + /messages/) ---
    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(scope, receive, send):
        async with sse_transport.connect_sse(scope, receive, send) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )

    async def handle_messages(scope, receive, send):
        await sse_transport.handle_post_message(scope, receive, send)

    # --- Streamable HTTP transport (recommended, /mcp) ---
    session_manager = StreamableHTTPSessionManager(app=mcp_server)

    # Raw ASGI app — no Starlette routing (avoids 307 redirects)
    secret_token = os.environ.get("MCP_SECRET_TOKEN", "")
    oauth_client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "mcp-client")
    oauth_client_secret = os.environ.get("MCP_OAUTH_CLIENT_SECRET", secret_token)
    ollama_upstream = os.environ.get("OLLAMA_UPSTREAM", "http://ollama:11434").rstrip("/")
    ollama_api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    protected_paths = {"/mcp", "/sse", "/messages", "/api/session/register",
                       "/api/session/list", "/api/session/update",
                       "/api/session/delete", "/api/connect",
                       "/api/identify",
                       "/api/user/connections"}
    public_paths = {"/health", "/.well-known/oauth-authorization-server",
                    "/oauth/token", "/oauth/register"}

    def _check_auth(headers):
        """Backward-compat: True if request has valid legacy MCP credentials.

        Used for paths where unified-auth is not yet enforced (Ollama
        passthrough fallback, OAuth flow). New-style Odoo-key auth is
        handled by `_check_auth_and_resolve` below.
        """
        if not secret_token:
            return True
        api_token = headers.get(b"x-api-token", b"").decode()
        if api_token and api_token == secret_token:
            return True
        auth = headers.get(b"authorization", b"").decode()
        if auth.startswith("Bearer "):
            bearer = auth[7:]
            if bearer == secret_token:
                return True
        return False

    def _check_auth_and_resolve(headers):
        """Auth check that also resolves the calling MCP user.

        Strategy:
          1. If headers carry the new unified-auth schema (X-Odoo-Url +
             Bearer + X-Odoo-Db + X-Odoo-Login), validate XMLRPC and
             resolve to a registered MCP user. Success → set ContextVar.
             Failure → reject (do NOT fall back to legacy — caller chose
             this schema explicitly).
          2. Otherwise, fall back to legacy `_check_auth` (no caller
             identity bound; tools rely on identify()/_session_users).

        Returns (ok: bool, caller: dict | None).
        """
        if headers.get(b"x-odoo-url"):
            caller = get_caller_odoo_user(headers)
            if caller:
                _odoo_caller_ctx.set(caller)
                return True, caller
            return False, None
        return _check_auth(headers), None

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            async with session_manager.run():
                # Handle lifespan events
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return

        path = scope.get("path", "")

        # ── Debug: log all headers on /mcp requests ────────────────
        if scope["type"] == "http" and path in ("/mcp", "/oauth/token", "/oauth/authorize", "/oauth/register"):
            hdrs = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            logger.info(f"[AUTH-DEBUG] {scope.get('method', '?')} {path} headers: {json.dumps(hdrs, indent=2)}")

        # ── OAuth 2.0 metadata ─────────────────────────────────────
        if path == "/.well-known/oauth-authorization-server" and scope["type"] == "http":
            from starlette.responses import JSONResponse
            host = dict(scope.get("headers", [])).get(b"host", b"localhost").decode()
            scheme = "https" if "443" in str(scope.get("server", ("", ""))) or host.endswith((".space", ".com", ".net")) else "http"
            base = f"{scheme}://{host}"
            response = JSONResponse({
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "client_credentials"],
                "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
                "code_challenge_methods_supported": ["S256"],
            })
            await response(scope, receive, send)
            return

        # ── OAuth token endpoint ───────────────────────────────────
        if path == "/oauth/token" and scope["type"] == "http":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.body()
                from urllib.parse import parse_qs
                params = parse_qs(body.decode())
                grant_type = params.get("grant_type", [None])[0]
                cid = params.get("client_id", [None])[0]
                csecret = params.get("client_secret", [None])[0]

                # Also check Basic auth header
                if not cid or not csecret:
                    import base64
                    auth_header = dict(scope.get("headers", [])).get(b"authorization", b"").decode()
                    if auth_header.startswith("Basic "):
                        decoded = base64.b64decode(auth_header[6:]).decode()
                        if ":" in decoded:
                            cid, csecret = decoded.split(":", 1)

                if grant_type == "client_credentials":
                    if cid == oauth_client_id and csecret == oauth_client_secret:
                        response = JSONResponse({
                            "access_token": secret_token,
                            "token_type": "Bearer",
                            "expires_in": 86400,
                        })
                    else:
                        response = JSONResponse(
                            {"error": "invalid_client"}, status_code=401)
                elif grant_type == "authorization_code":
                    code = params.get("code", [None])[0]
                    if code == secret_token:
                        response = JSONResponse({
                            "access_token": secret_token,
                            "token_type": "Bearer",
                            "expires_in": 86400,
                        })
                    else:
                        response = JSONResponse(
                            {"error": "invalid_grant"}, status_code=400)
                else:
                    response = JSONResponse(
                        {"error": "unsupported_grant_type"}, status_code=400)
            except Exception as e:
                response = JSONResponse(
                    {"error": "server_error", "error_description": str(e)},
                    status_code=500)
            await response(scope, receive, send)
            return

        # ── OAuth authorize (redirect with code) ───────────────────
        if path == "/oauth/authorize" and scope["type"] == "http":
            from starlette.requests import Request
            from starlette.responses import RedirectResponse, JSONResponse
            request = Request(scope, receive)
            redirect_uri = request.query_params.get("redirect_uri", "")
            state = request.query_params.get("state", "")
            if redirect_uri:
                sep = "&" if "?" in redirect_uri else "?"
                target = f"{redirect_uri}{sep}code={secret_token}"
                if state:
                    target += f"&state={state}"
                response = RedirectResponse(target)
            else:
                response = JSONResponse({"error": "missing redirect_uri"}, status_code=400)
            await response(scope, receive, send)
            return

        # ── OAuth dynamic client registration ──────────────────────
        if path == "/oauth/register" and scope["type"] == "http" and scope.get("method") == "POST":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.json()
                response = JSONResponse({
                    "client_id": oauth_client_id,
                    "client_secret": oauth_client_secret,
                    "client_name": body.get("client_name", "mcp-client"),
                    "redirect_uris": body.get("redirect_uris", []),
                    "grant_types": ["authorization_code", "client_credentials"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "client_secret_post",
                }, status_code=201)
            except Exception:
                response = JSONResponse({"error": "invalid_request"}, status_code=400)
            await response(scope, receive, send)
            return

        # ── Authentication for protected paths ─────────────────────
        # Unified-auth: try Odoo API-key validation first (sets ContextVar
        # so tool handlers can read the validated caller). Falls back to
        # legacy secret_token auth when the request does not carry the
        # X-Odoo-* schema. Runs even without secret_token configured so
        # ContextVar gets set for downstream tools.
        if scope["type"] == "http":
            check_path = path.rstrip("/")
            needs_auth = (check_path in protected_paths
                          or check_path.startswith("/messages/"))
            is_public = check_path in public_paths
            if needs_auth and not is_public:
                headers = dict(scope.get("headers", []))
                # Skip enforcement entirely if no auth is configured AND
                # the caller is not using the unified schema.
                if secret_token or headers.get(b"x-odoo-url"):
                    ok, _caller = _check_auth_and_resolve(headers)
                    if not ok:
                        from starlette.responses import JSONResponse
                        host = headers.get(b"host", b"localhost").decode()
                        response = JSONResponse(
                            {"error": "Unauthorized",
                             "hint": "Use 'Authorization: Bearer <odoo_api_key>' + "
                                     "X-Odoo-Url/Db/Login, or legacy X-Api-Token. "
                                     "Register key first via POST /api/user/register-connection."},
                            status_code=401,
                            headers={"WWW-Authenticate": f'Bearer resource_metadata="https://{host}/.well-known/oauth-authorization-server"'})
                        await response(scope, receive, send)
                        return

        # ── Ollama passthrough (/ollama/* → OLLAMA_UPSTREAM) ───────
        if scope["type"] == "http" and (path == "/ollama" or path.startswith("/ollama/")):
            import httpx
            headers_dict = dict(scope.get("headers", []))

            # Dedicated auth — independent from MCP_SECRET_TOKEN.
            # If OLLAMA_API_KEY is set, require it (Bearer or X-Api-Token).
            # If not set, fall back to MCP auth (_check_auth) for safety.
            def _ollama_authorized(hdrs):
                if ollama_api_key:
                    auth = hdrs.get(b"authorization", b"").decode()
                    if auth.startswith("Bearer ") and auth[7:] == ollama_api_key:
                        return True
                    api_token = hdrs.get(b"x-api-token", b"").decode()
                    if api_token and api_token == ollama_api_key:
                        return True
                    return False
                # No dedicated key → reuse MCP auth (returns True if neither set)
                return _check_auth(hdrs)

            if not _ollama_authorized(headers_dict):
                from starlette.responses import JSONResponse
                response = JSONResponse(
                    {"error": "Unauthorized",
                     "hint": "Use 'Authorization: Bearer <OLLAMA_API_KEY>' or 'X-Api-Token' header"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return

            upstream_path = path[len("/ollama"):] or "/"
            query = scope.get("query_string", b"").decode()
            upstream_url = f"{ollama_upstream}{upstream_path}"
            if query:
                upstream_url += f"?{query}"

            # Read full request body (ollama endpoints are not streaming uploads)
            body = b""
            more_body = True
            while more_body:
                msg = await receive()
                if msg.get("type") == "http.disconnect":
                    return
                body += msg.get("body", b"")
                more_body = msg.get("more_body", False)

            # Strip hop-by-hop + auth/host headers
            drop_headers = {b"host", b"authorization", b"x-api-token",
                            b"connection", b"transfer-encoding",
                            b"content-length", b"upgrade"}
            fwd_headers = {
                k.decode(): v.decode()
                for k, v in scope.get("headers", [])
                if k.lower() not in drop_headers
            }

            method = scope.get("method", "GET")
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    upstream_resp = await client.request(
                        method, upstream_url,
                        headers=fwd_headers,
                        content=body if body else None,
                    )
                resp_headers = [
                    (k.lower().encode(), v.encode())
                    for k, v in upstream_resp.headers.items()
                    if k.lower() not in (
                        "transfer-encoding", "content-encoding",
                        "connection", "content-length",
                    )
                ]
                await send({
                    "type": "http.response.start",
                    "status": upstream_resp.status_code,
                    "headers": resp_headers,
                })
                await send({
                    "type": "http.response.body",
                    "body": upstream_resp.content,
                    "more_body": False,
                })
            except httpx.TimeoutException:
                from starlette.responses import JSONResponse
                response = JSONResponse(
                    {"error": "Upstream timeout", "upstream": ollama_upstream},
                    status_code=504,
                )
                await response(scope, receive, send)
            except Exception as e:
                logger.exception(f"Ollama passthrough error: {e}")
                from starlette.responses import JSONResponse
                response = JSONResponse(
                    {"error": f"Upstream error: {e}"},
                    status_code=502,
                )
                await response(scope, receive, send)
            return

        # ── Landing page ───────────────────────────────────────────
        if path in ("/", "") and scope["type"] == "http":
            from starlette.responses import HTMLResponse
            host = dict(scope.get("headers", [])).get(b"host", b"localhost").decode()
            cover = "https://www.bl-consulting.net/web/image/63493-d3390658/Blog%20Post%20%27Running%20Odoo%2018%20Entirely%20Through%20AI%20%E2%80%94%20A%20Live%20Claude%20Code%20%2B%20MCP%20Demo%27%20cover%20image.webp"
            html = f"""<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Odoo RPC MCP Server &mdash; BL Consulting</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Inter', sans-serif; color: #fff; min-height: 100vh;
    background-image: url('{cover}');
    background-position: right center;
    background-size: cover;
    background-repeat: no-repeat;
    background-attachment: fixed;
  }}

  /* ── Hero header ── */
  .hero {{
    position: relative; width: 100%; min-height: 420px;
    display: flex; align-items: flex-end;
  }}
  .hero-overlay {{
    position: absolute; inset: 0;
    background: linear-gradient(180deg, rgba(113,75,160,0.2) 0%, rgba(113,75,160,0.75) 100%);
  }}
  .hero-content {{
    position: relative; z-index: 1; width: 100%; max-width: 960px;
    margin: 0 auto; padding: 48px 32px;
  }}
  .hero h1 {{ font-size: 2.4em; font-weight: 700; color: #fff; margin-bottom: 8px;
              text-shadow: 0 2px 8px rgba(0,0,0,0.3); }}
  .hero p {{ color: rgba(255,255,255,0.9); font-size: 1.15em; text-shadow: 0 1px 4px rgba(0,0,0,0.3); }}
  .hero .badge {{
    display: inline-block; background: #21b6b7; color: #fff; padding: 5px 16px;
    border-radius: 20px; font-size: 0.85em; font-weight: 600; margin-top: 16px;
  }}

  /* ── Odoo-style nav bar ── */
  .navbar {{
    background: rgba(113,75,160,0.85); backdrop-filter: blur(10px);
    padding: 0 32px; display: flex; align-items: center;
    height: 46px; max-width: 100%; position: sticky; top: 0; z-index: 100;
  }}
  .navbar a {{
    color: rgba(255,255,255,0.8); text-decoration: none; font-size: 0.9em;
    font-weight: 500; padding: 12px 16px; transition: color 0.2s;
  }}
  .navbar a:hover {{ color: #fff; }}
  .navbar .brand {{ font-weight: 700; color: #fff; font-size: 1em; margin-right: auto; }}

  /* ── Content area ── */
  .content {{ max-width: 960px; margin: 0 auto; padding: 40px 32px; }}

  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin: 24px 0; }}
  .card {{
    background: rgba(255,255,255,0.12); backdrop-filter: blur(16px);
    border-radius: 12px; padding: 24px;
    border: 1px solid rgba(255,255,255,0.15); transition: all 0.3s;
  }}
  .card:hover {{ background: rgba(255,255,255,0.18); box-shadow: 0 8px 32px rgba(0,0,0,0.2); transform: translateY(-2px); }}
  .card h3 {{ font-size: 1.05em; color: #d4b8ff; margin-bottom: 8px; }}
  .card p {{ font-size: 0.9em; color: rgba(255,255,255,0.8); line-height: 1.6; }}
  .card code {{ background: rgba(255,255,255,0.15); padding: 2px 8px; border-radius: 4px; font-size: 0.85em; color: #d4b8ff; }}

  /* ── Endpoint list ── */
  .endpoints {{ margin: 24px 0; }}
  .ep {{
    display: flex; align-items: center; gap: 12px; padding: 14px 20px;
    background: rgba(255,255,255,0.1); backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; margin: 8px 0;
  }}
  .ep .method {{
    background: rgba(113,75,160,0.8); color: #fff; padding: 3px 10px; border-radius: 4px;
    font-size: 0.8em; font-weight: 600; font-family: monospace; min-width: 52px; text-align: center;
  }}
  .ep .method.get {{ background: rgba(33,182,183,0.8); }}
  .ep .path {{ font-family: monospace; font-weight: 600; color: #fff; }}
  .ep .desc {{ color: rgba(255,255,255,0.6); font-size: 0.85em; margin-left: auto; }}

  /* ── Setup box ── */
  .setup-box {{
    background: rgba(113,75,160,0.5); backdrop-filter: blur(16px);
    border: 1px solid rgba(255,255,255,0.15);
    color: #fff; padding: 32px; border-radius: 16px; margin: 32px 0;
  }}
  .setup-box h2 {{ font-size: 1.3em; margin-bottom: 16px; }}
  .setup-box ol {{ padding-left: 20px; line-height: 2.2; }}
  .setup-box code {{ background: rgba(255,255,255,0.2); padding: 2px 8px; border-radius: 4px; color: #fff; }}

  /* ── Footer ── */
  .footer {{
    text-align: center; padding: 32px; color: rgba(255,255,255,0.5); font-size: 0.85em;
    border-top: 1px solid rgba(255,255,255,0.1); margin-top: 40px;
  }}
  .footer a {{ color: #d4b8ff; text-decoration: none; }}
  .footer a:hover {{ text-decoration: underline; }}

  h2 {{ font-size: 1.4em; color: #fff; margin: 32px 0 16px; font-weight: 600;
       text-shadow: 0 1px 4px rgba(0,0,0,0.3); }}
  h2 span {{ color: #d4b8ff; }}
</style>
</head>
<body>

<nav class="navbar">
  <span class="brand">BL Consulting</span>
  <a href="https://www.bl-consulting.net">Website</a>
  <a href="https://github.com/rosenvladimirov/odoo-claude-mcp">GitHub</a>
  <a href="https://{host}/health">Health</a>
</nav>

<div class="hero">
  <div class="hero-overlay"></div>
  <div class="hero-content">
    <h1>Odoo RPC MCP Server</h1>
    <p>Model Context Protocol server &mdash; connect Claude AI directly to Odoo 18</p>
    <span class="badge">&check; Online</span>
  </div>
</div>

<div class="content">

  <h2><span>&rsaquo;</span> Endpoints</h2>
  <div class="endpoints">
    <div class="ep"><span class="method">POST</span><span class="path">/mcp</span><span class="desc">Streamable HTTP (MCP protocol)</span></div>
    <div class="ep"><span class="method get">GET</span><span class="path">/sse</span><span class="desc">SSE transport (legacy)</span></div>
    <div class="ep"><span class="method get">GET</span><span class="path">/health</span><span class="desc">Health check (public)</span></div>
    <div class="ep"><span class="method get">GET</span><span class="path">/.well-known/oauth-authorization-server</span><span class="desc">OAuth 2.0</span></div>
  </div>

  <div class="setup-box">
    <h2>Setup in Claude.ai</h2>
    <ol>
      <li><strong>Settings &rarr; Connectors &rarr; Add custom connector</strong></li>
      <li>URL: <code>https://{host}/mcp</code></li>
      <li>Enter your <strong>OAuth Client ID</strong> and <strong>Client Secret</strong></li>
      <li>Start a conversation: <code>identify("your name")</code></li>
    </ol>
  </div>

  <h2><span>&rsaquo;</span> Features</h2>
  <div class="cards">
    <div class="card">
      <h3>Odoo ERP</h3>
      <p>40+ tools: CRUD operations, reports, fiscal positions, search, execute methods. Full XML-RPC &amp; JSON-RPC support.</p>
    </div>
    <div class="card">
      <h3>Infrastructure</h3>
      <p>SSH remote execution, Git operations on remote servers. Manage Docker via Portainer integration.</p>
    </div>
    <div class="card">
      <h3>Integrations</h3>
      <p>Google Calendar &amp; Gmail, Telegram messaging. OAuth 2.0 authentication for secure access.</p>
    </div>
    <div class="card">
      <h3>Multi-user</h3>
      <p>Per-user connections with session isolation. <code>identify()</code> to load personal settings. Lock management for shared resources.</p>
    </div>
  </div>

</div>

<div class="footer">
  <a href="https://www.bl-consulting.net">BL Consulting</a> &middot;
  <a href="https://github.com/rosenvladimirov/odoo-claude-mcp">GitHub</a> &middot;
  Powered by <a href="https://modelcontextprotocol.io">MCP</a> &amp;
  <a href="https://www.odoo.com">Odoo</a>
</div>

</body>
</html>"""
            response = HTMLResponse(html)
            await response(scope, receive, send)
            return

        if path == "/health" and scope["type"] == "http":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            response = JSONResponse({
                "status": "ok",
                "service": "odoo-rpc-mcp",
                "connections": len(manager.connections) if manager else 0,
                "aliases": list(manager.connections.keys()) if manager else [],
                "timestamp": datetime.now().isoformat(),
            })
            await response(scope, receive, send)
        elif path.startswith("/admin/memory/") and scope["type"] == "http":
            # Licensed memory admin endpoints — used by the Odoo billing
            # module (ai.billing.memory.deployment) to push purchased
            # memory-pack .md files into a tenant's licensed folder.
            # Auth: shared secret in env MCP_ADMIN_TOKEN.
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            expected = os.environ.get("MCP_ADMIN_TOKEN", "")
            provided = (request.headers.get("authorization") or "").replace(
                "Bearer ", "", 1,
            ).strip()
            if not expected or provided != expected:
                response = JSONResponse(
                    {"error": "admin auth required"}, status_code=401,
                )
                await response(scope, receive, send)
                return
            try:
                import base64 as _b64
                method = scope.get("method", "")
                if path == "/admin/memory/upload" and method == "POST":
                    body = await request.json()
                    tenant_code = (body.get("tenant_code") or "").strip()
                    filename = os.path.basename(body.get("filename") or "")
                    content_b64 = body.get("content_b64") or ""
                    if not (tenant_code and filename and content_b64):
                        response = JSONResponse(
                            {"error": "tenant_code, filename, content_b64 required"},
                            status_code=400,
                        )
                        await response(scope, receive, send)
                        return
                    if not filename.endswith(".md"):
                        filename += ".md"
                    try:
                        content = _b64.b64decode(content_b64)
                    except Exception as e:  # noqa: BLE001
                        response = JSONResponse(
                            {"error": f"invalid base64: {e}"}, status_code=400,
                        )
                        await response(scope, receive, send)
                        return
                    target_dir = _memory_licensed_dir(tenant_code, create=True)
                    fpath = os.path.join(target_dir, filename)
                    existed = os.path.isfile(fpath)
                    with open(fpath, "wb") as f:
                        f.write(content)
                    response = JSONResponse({
                        "status": "updated" if existed else "created",
                        "tenant_code": tenant_code,
                        "filename": filename,
                        "size": len(content),
                    })
                elif path == "/admin/memory/remove" and method == "POST":
                    body = await request.json()
                    tenant_code = (body.get("tenant_code") or "").strip()
                    filename = os.path.basename(body.get("filename") or "")
                    if not (tenant_code and filename):
                        response = JSONResponse(
                            {"error": "tenant_code + filename required"}, status_code=400,
                        )
                        await response(scope, receive, send)
                        return
                    if not filename.endswith(".md"):
                        filename += ".md"
                    fpath = os.path.join(_memory_licensed_dir(tenant_code), filename)
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                        response = JSONResponse({"status": "removed", "filename": filename})
                    else:
                        response = JSONResponse(
                            {"status": "not_found", "filename": filename},
                            status_code=404,
                        )
                elif path == "/admin/memory/list" and method == "GET":
                    qs = request.query_params
                    tenant_code = (qs.get("tenant_code") or "").strip()
                    if not tenant_code:
                        response = JSONResponse(
                            {"error": "tenant_code query param required"}, status_code=400,
                        )
                        await response(scope, receive, send)
                        return
                    files = _memory_list_files(_memory_licensed_dir(tenant_code))
                    response = JSONResponse({
                        "tenant_code": tenant_code,
                        "count": len(files),
                        "files": files,
                    })
                else:
                    response = JSONResponse(
                        {"error": f"unknown admin memory endpoint: {method} {path}"},
                        status_code=404,
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("admin/memory handler failed")
                response = JSONResponse({"error": str(e)[:400]}, status_code=500)
            await response(scope, receive, send)
        elif path == "/api/session/register" and scope["type"] == "http" and scope.get("method") == "POST":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.json()
                sid = session_mgr.register(
                    connection_alias=body.get("connection_alias", "default"),
                    odoo_url=body.get("odoo_url", ""),
                    odoo_db=body.get("odoo_db", ""),
                    odoo_username=body.get("odoo_username", ""),
                    model=body.get("model", ""),
                    res_id=int(body.get("res_id", 0) or 0),
                    view_type=body.get("view_type", ""),
                    terminal_url=body.get("terminal_url", ""),
                )
                response = JSONResponse({"status": "registered", "session_id": sid})
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/api/session/list" and scope["type"] == "http" and scope.get("method") == "GET":
            from starlette.responses import JSONResponse
            try:
                response = JSONResponse({"sessions": session_mgr.list_all()})
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/api/session/update" and scope["type"] == "http" and scope.get("method") == "POST":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.json()
                sid = body.get("session_id")
                ok = session_mgr.update_context(
                    sid,
                    model=body.get("model"),
                    res_id=int(body["res_id"]) if "res_id" in body else None,
                    view_type=body.get("view_type"),
                )
                response = JSONResponse({"status": "ok" if ok else "not_found"})
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/api/session/delete" and scope["type"] == "http" and scope.get("method") == "POST":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.json()
                ok = session_mgr.delete(body.get("session_id", ""))
                response = JSONResponse({"status": "deleted" if ok else "not_found"})
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/api/connect" and scope["type"] == "http" and scope.get("method") == "POST":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.json()
                conn = manager.add(
                    alias="default",
                    url=body.get("url", ""),
                    db=body.get("db", ""),
                    username=body.get("username", "admin"),
                    password=body.get("password", ""),
                    api_key=body.get("api_key", ""),
                    protocol=body.get("protocol", "xmlrpc"),
                )
                uid = conn.authenticate()
                response = JSONResponse({"status": "connected", "uid": uid, **conn.to_dict()})
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/api/identify" and scope["type"] == "http" and scope.get("method") == "POST":
            # Unified-auth-aware: if middleware validated the caller,
            # identity comes from ContextVar (spoof-proof). Only when no
            # HTTP auth was enforced (no secret_token, no X-Odoo-*) we
            # fall back to body.name for dev/local compatibility.
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.json() if await request.body() else {}
            except Exception:
                body = {}
            caller = _odoo_caller_ctx.get()
            try:
                if caller:
                    user_name = caller["mcp_user"]
                    safe_name = user_name
                    preferred_alias = caller.get("alias")
                else:
                    user_name = (body.get("name") or "").strip()
                    if not user_name:
                        response = JSONResponse(
                            {"error": "name required (or use unified-auth headers)"},
                            status_code=400)
                        await response(scope, receive, send)
                        return
                    safe_name = _sanitize_name(user_name)
                    preferred_alias = None

                is_new = not os.path.isdir(os.path.join(DATA_DIR, "users", safe_name))
                _session_users["current"] = user_name
                conns = _load_user_connections(user_name)
                active = _load_user_active(user_name)
                alias_to_activate = preferred_alias or active.get("alias")
                if alias_to_activate and alias_to_activate in conns:
                    c = conns[alias_to_activate]
                    try:
                        conn = manager.add(
                            alias="default",
                            url=c["url"], db=c["db"],
                            username=c["user"],
                            api_key=c.get("api_key", ""),
                            password=c.get("password", ""),
                            protocol=c.get("protocol", "xmlrpc"),
                        )
                        conn.authenticate()
                    except Exception:
                        pass
                response = JSONResponse({
                    "status": "new_profile" if is_new else "identified",
                    "user": user_name,
                    "profile": safe_name,
                    "data_dir": f"/data/users/{safe_name}",
                    "memory_dir": f"/data/memory/users/{safe_name}",
                    "connections": list(conns.keys()),
                    "active": alias_to_activate,
                    "validated": bool(caller),
                })
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/api/user/register-connection" and scope["type"] == "http" and scope.get("method") == "POST":
            # Self-registering endpoint for a single (alias → url/db/login/api_key)
            # binding under an MCP user profile. Authenticates via XMLRPC validation
            # of the supplied key — no separate token required.
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            try:
                body = await request.json()
                name = (body.get("name") or "").strip()
                alias = (body.get("alias") or "").strip()
                url = (body.get("url") or "").strip().rstrip("/")
                db = (body.get("db") or "").strip()
                login = (body.get("login") or "").strip()
                api_key = (body.get("api_key") or "").strip()
                make_active = bool(body.get("active"))
                if not all([name, alias, url, db, login, api_key]):
                    response = JSONResponse(
                        {"error": "name, alias, url, db, login, api_key are required"},
                        status_code=400)
                    await response(scope, receive, send)
                    return

                uid = _xmlrpc_validate(url, db, login, api_key)
                if not uid:
                    response = JSONResponse(
                        {"error": "Invalid Odoo credentials — XMLRPC authenticate failed"},
                        status_code=401)
                    await response(scope, receive, send)
                    return

                # Conflict: same 4-tuple already bound to a different profile?
                existing = _resolve_mcp_user(url, db, login, api_key)
                if existing and existing[0] != _sanitize_name(name):
                    response = JSONResponse(
                        {"error": "Connection already bound to another MCP profile",
                         "owner": existing[0]},
                        status_code=409)
                    await response(scope, receive, send)
                    return

                # Ownership proof for non-empty existing profile:
                # caller must already have at least one connection in this
                # profile with the same login (email-style identity match).
                # This lets an owner add new Odoo instances (different
                # url+db) but blocks an unrelated identity from hijacking
                # a profile name.
                conns = _load_user_connections(name) or {}
                if conns:
                    proves_ownership = any(
                        c.get("user") == login for c in conns.values()
                    )
                    if not proves_ownership:
                        response = JSONResponse(
                            {"error": "Profile already owned by another identity. "
                                      "Cannot bind unrelated Odoo login."},
                            status_code=403)
                        await response(scope, receive, send)
                        return

                conns[alias] = {
                    "url": url, "db": db, "user": login,
                    "api_key": api_key, "uid": uid,
                    "protocol": body.get("protocol", "xmlrpc"),
                }
                _save_user_connections(name, conns)
                if make_active:
                    _save_user_active(name, {"alias": alias})

                # Invalidate cache for this 4-tuple so next call picks up
                # the new mapping (e.g. alias rename, key rotation).
                cache_key = _auth_hashlib.sha256(
                    f"{url}|{db}|{login}|{api_key}".encode()).hexdigest()
                with _auth_cache_lock:
                    _auth_cache.pop(cache_key, None)

                response = JSONResponse({
                    "status": "registered",
                    "user": name,
                    "profile": _sanitize_name(name),
                    "alias": alias,
                    "uid": uid,
                    "active": alias if make_active else None,
                })
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/api/user/connections" and scope["type"] == "http":
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            request = Request(scope, receive)
            method = scope.get("method", "GET")
            try:
                if method == "GET":
                    # GET /api/user/connections?name=Rosen
                    name = request.query_params.get("name", "")
                    if not name:
                        response = JSONResponse({"error": "name parameter required"}, status_code=400)
                    else:
                        conns = _load_user_connections(name)
                        active = _load_user_active(name)
                        response = JSONResponse({
                            "user": name,
                            "profile": _sanitize_name(name),
                            "connections": conns,
                            "active": active.get("alias"),
                        })
                elif method == "POST":
                    # POST /api/user/connections — save full connections dict
                    body = await request.json()
                    name = body.get("name", "")
                    connections = body.get("connections", {})
                    if not name:
                        response = JSONResponse({"error": "name required"}, status_code=400)
                    elif not isinstance(connections, dict):
                        response = JSONResponse({"error": "connections must be a dict"}, status_code=400)
                    else:
                        _save_user_connections(name, connections)
                        # Optionally save active connection
                        if body.get("active"):
                            _save_user_active(name, {"alias": body["active"]})
                        response = JSONResponse({
                            "status": "saved",
                            "user": name,
                            "profile": _sanitize_name(name),
                            "count": len(connections),
                        })
                else:
                    response = JSONResponse({"error": "Method not allowed"}, status_code=405)
            except Exception as e:
                response = JSONResponse({"error": str(e)}, status_code=400)
            await response(scope, receive, send)
        elif path == "/mcp":
            await session_manager.handle_request(scope, receive, send)
        elif path == "/sse":
            await handle_sse(scope, receive, send)
        elif path.startswith("/messages"):
            await handle_messages(scope, receive, send)
        else:
            from starlette.responses import Response
            response = Response("Not Found", status_code=404)
            await response(scope, receive, send)
    return app


if __name__ == "__main__":
    app = create_app()

    # ── Discover proxy tools from sub-services (with retry) ──
    import threading
    def _startup_discover():
        """Discover proxy tools with retries until all services respond."""
        import time
        services = _get_proxy_services()
        missing = set(services.keys())
        for attempt in range(6):
            time.sleep(5 + attempt * 2)
            try:
                _discover_proxy_tools(only_services=missing)
                found = {v["service"] for v in PROXY_TOOLS.values()}
                missing = set(services.keys()) - found
                logger.info(f"Proxy attempt {attempt+1}: {len(PROXY_TOOLS)} tools, missing={missing or 'none'}")
                if not missing:
                    break
            except Exception as e:
                logger.warning(f"Proxy attempt {attempt+1}: {e}")
        logger.info(f"Proxy startup done: {len(PROXY_TOOLS)} total proxied tools")

    threading.Thread(target=_startup_discover, daemon=True).start()

    logger.info(f"Odoo RPC MCP server starting on {MCP_HOST}:{MCP_PORT}")
    logger.info(f"  Streamable HTTP: http://{MCP_HOST}:{MCP_PORT}/mcp")
    logger.info(f"  SSE (legacy):    http://{MCP_HOST}:{MCP_PORT}/sse")
    logger.info(f"  Health:          http://{MCP_HOST}:{MCP_PORT}/health")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
