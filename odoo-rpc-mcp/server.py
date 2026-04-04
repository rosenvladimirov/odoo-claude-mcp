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
import sys
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

# ─── Configuration ──────────────────────────────────────────
MCP_PORT = int(os.environ.get("MCP_PORT", "8084"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
CONNECTIONS_FILE = Path(os.environ.get("CONNECTIONS_FILE", "/data/connections.json"))
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
google_mgr: GoogleServiceManager | None = None
telegram_mgr: TelegramServiceManager | None = None
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
]


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    if SINGLE_CONNECTION:
        return [t for t in TOOLS if t.name != "odoo_disconnect"]
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
    global manager, google_mgr

    manager = ConnectionManager(CONNECTIONS_FILE)
    logger.info(f"Loaded {len(manager.connections)} connection(s): {list(manager.connections.keys())}")

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
    logger.info(f"Odoo RPC MCP server starting on {MCP_HOST}:{MCP_PORT}")
    logger.info(f"  Streamable HTTP: http://{MCP_HOST}:{MCP_PORT}/mcp")
    logger.info(f"  SSE (legacy):    http://{MCP_HOST}:{MCP_PORT}/sse")
    logger.info(f"  Health:          http://{MCP_HOST}:{MCP_PORT}/health")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
