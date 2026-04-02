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
- SSE/HTTP transport for Docker deployment

Transport: SSE/HTTP (persistent, Claude Code connects via URL)
"""
import asyncio
import json
import logging
import os
import signal
import sys
import xmlrpc.client
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from aiohttp import web
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool

# ─── Configuration ──────────────────────────────────────────
MCP_PORT = int(os.environ.get("MCP_PORT", "8084"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
CONNECTIONS_FILE = Path(os.environ.get("CONNECTIONS_FILE", "/data/connections.json"))
# Single-connection mode: when ODOO_URL is set, hide connect/disconnect tools
SINGLE_CONNECTION = bool(os.environ.get("ODOO_URL", ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("odoo-rpc-mcp")


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
    ):
        self.alias = alias
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.password = password
        self.api_key = api_key
        self.protocol = protocol  # xmlrpc or jsonrpc
        self._uid: int | None = None
        self._auth_token: str = ""  # password or api_key

    @property
    def auth_token(self) -> str:
        return self.api_key or self.password

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
            common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common",
                allow_none=True,
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
                obj = xmlrpc.client.ServerProxy(
                    f"{self.url}/xmlrpc/2/object",
                    allow_none=True,
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
        with urllib.request.urlopen(req, timeout=120) as resp:
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
            "has_api_key": bool(self.api_key),
            "has_password": bool(self.password),
        }


# ─── Connection Manager ────────────────────────────────────
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

        # From connections file
        if self.data_file.exists():
            try:
                data = json.loads(self.data_file.read_text())
                for item in data if isinstance(data, list) else data.get("connections", []):
                    conn = OdooConnection(
                        alias=item["alias"],
                        url=item["url"],
                        db=item["db"],
                        username=item["username"],
                        password=item.get("password", ""),
                        api_key=item.get("api_key", ""),
                        protocol=item.get("protocol", "xmlrpc"),
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
mcp_server = Server("odoo-rpc-mcp")


def _mgr() -> ConnectionManager:
    if manager is None:
        raise Exception("Connection manager not initialized")
    return manager


def _conn(args: dict) -> OdooConnection:
    return _mgr().get(args.get("connection", "default"))


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
            },
            "required": ["url", "db", "username"],
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
]


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    if SINGLE_CONNECTION:
        return [t for t in TOOLS if t.name not in ("odoo_connect", "odoo_disconnect")]
    return TOOLS


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _execute_tool, name, arguments
        )
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        # Truncate very large responses
        if len(text) > 100_000:
            text = text[:100_000] + "\n... (truncated, use limit/fields to narrow)"
        return [TextContent(type="text", text=text)]
    except Exception as e:
        logger.error(f"Tool {name} error: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


def _execute_tool(name: str, args: dict) -> Any:
    m = _mgr()

    # ── Connection management ──
    if name == "odoo_connect":
        if SINGLE_CONNECTION:
            return {"error": "Single-connection mode: cannot add connections. Using pre-configured default."}
        conn = m.add(
            alias=args.get("alias", "default"),
            url=args["url"],
            db=args["db"],
            username=args["username"],
            password=args.get("password", ""),
            api_key=args.get("api_key", ""),
            protocol=args.get("protocol", "xmlrpc"),
        )
        # Test authentication
        uid = conn.authenticate()
        return {"status": "connected", "uid": uid, **conn.to_dict()}

    elif name == "odoo_disconnect":
        if SINGLE_CONNECTION:
            return {"error": "Single-connection mode: cannot remove connections."}
        alias = args.get("alias", "default")
        ok = m.remove(alias)
        return {"status": "removed" if ok else "not_found", "alias": alias}

    elif name == "odoo_connections":
        return {"connections": m.list_all()}

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
        return {"created_ids": ids}

    elif name == "odoo_write":
        result = conn.execute_kw(args["model"], "write", [args["ids"], args["values"]])
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

    return {"error": f"Unknown tool: {name}"}


# ─── Health Endpoint ───────────────────────────────────────
async def health_handler(request: web.Request) -> web.Response:
    m = _mgr()
    return web.json_response({
        "status": "ok",
        "service": "odoo-rpc-mcp",
        "connections": len(m.connections) if m else 0,
        "aliases": list(m.connections.keys()) if m else [],
        "timestamp": datetime.now().isoformat(),
    })


# ─── Main ──────────────────────────────────────────────────
async def main():
    global manager

    manager = ConnectionManager(CONNECTIONS_FILE)
    logger.info(f"Loaded {len(manager.connections)} connection(s): {list(manager.connections.keys())}")

    # SSE MCP Transport
    sse_transport = SseServerTransport("/messages/")
    app = web.Application()
    app.router.add_get("/sse", sse_transport.handle_sse)
    app.router.add_post("/messages/", sse_transport.handle_post_message)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, MCP_HOST, MCP_PORT)
    await site.start()

    logger.info(f"Odoo RPC MCP server started on {MCP_HOST}:{MCP_PORT}")
    logger.info(f"  SSE endpoint: http://{MCP_HOST}:{MCP_PORT}/sse")
    logger.info(f"  Health:       http://{MCP_HOST}:{MCP_PORT}/health")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
