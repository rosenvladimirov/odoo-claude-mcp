"""One-shot migration — force-revoke legacy v3 API keys.

Run AFTER deploying the RBAC-enabled api_key_manager (3.0.0-alpha.2+) but
BEFORE accepting any production /provision or /destroy traffic. Snapshots
the audit log first; for every active record without RBAC fields (no
``role``) or with an argon2 hash (legacy ``$argon2id$...``), it appends a
revocation record and prints a summary so the operator can manually
re-issue replacements via ``provision_issue_api_key``.

Auto-promotion to admin is intentionally NOT performed: any leaked legacy
key would otherwise gain max privilege. Manual re-issue is the audit
checkpoint.

Usage (inside the v3 container):
  python3 /app/migrate_legacy_keys.py
or via docker:
  docker exec mcp-odoo-rpc python3 /app/migrate_legacy_keys.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

API_KEYS_FILE = Path(os.environ.get("API_KEYS_FILE", "/data/api_keys.jsonl"))


def _is_legacy(rec: dict) -> bool:
    if rec.get("status") != "active":
        return False
    if "role" not in rec or "scope" not in rec:
        return True
    if (rec.get("key_hash") or "").startswith("$argon2"):
        return True
    return False


def _replay() -> dict[str, dict]:
    keys: dict[str, dict] = {}
    if not API_KEYS_FILE.is_file():
        return keys
    with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kid = rec.get("key_id")
            if not kid:
                continue
            if rec.get("status") == "revoked":
                keys.pop(kid, None)
            else:
                keys[kid] = rec
    return keys


def _append(record: dict) -> None:
    with open(API_KEYS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(API_KEYS_FILE, 0o600)
    except OSError:
        pass


def main() -> int:
    if not API_KEYS_FILE.is_file():
        print(f"no audit log at {API_KEYS_FILE}; nothing to do")
        return 0

    snapshot = API_KEYS_FILE.with_suffix(
        f".jsonl.bak-{int(time.time())}"
    )
    shutil.copy2(API_KEYS_FILE, snapshot)
    print(f"snapshot -> {snapshot}")

    keys = _replay()
    legacy = [(kid, rec) for kid, rec in keys.items() if _is_legacy(rec)]

    if not legacy:
        print("no legacy keys; migration not needed")
        return 0

    print(f"found {len(legacy)} legacy active key(s):")
    for kid, rec in legacy:
        print(f"  - {kid}  email={rec.get('email','?')}  "
              f"hash_prefix={(rec.get('key_hash') or '')[:12]!r}")

    now = int(time.time())
    for kid, _rec in legacy:
        _append({
            "key_id": kid,
            "status": "revoked",
            "revoked_ts": now,
            "reason": "rbac_migration: legacy schema (no role/scope or argon2 hash)",
        })
    print(f"\nrevoked {len(legacy)} legacy key(s).")
    print("\nNEXT STEPS (manual):")
    print("  1. Issue replacement admin keys via the MCP admin tool:")
    print("       provision_issue_api_key(email='you@x', role='admin')")
    print("  2. Re-distribute new keys to client Odoo instances.")
    print("  3. Verify with:")
    print("       provision_list_api_keys()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
