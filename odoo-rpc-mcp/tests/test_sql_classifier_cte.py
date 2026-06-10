"""B.0-3: SQL classifier must not let data-modifying CTEs (and multi-statement,
SELECT INTO, comment-boundary tricks) pass as harmless reads for USER role.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sql_classifier as C  # noqa: E402


def _role(q):
    return C.classify_sql(q)["role_required"]


# ── proven bypasses must now require admin ──
def test_cte_delete_protected_denied():
    info = C.classify_sql(
        "WITH t AS (DELETE FROM res_users RETURNING id) SELECT * FROM t")
    assert info["role_required"] == "admin"
    assert info["op"] in ("write_in_read", "ddl")


def test_cte_update_denied():
    assert _role(
        "WITH t AS (UPDATE res_users SET active=false RETURNING id) SELECT * FROM t"
    ) == "admin"


def test_cte_insert_denied():
    assert _role(
        "WITH t AS (INSERT INTO res_users(login) VALUES('x') RETURNING id) SELECT * FROM t"
    ) == "admin"


def test_cte_delete_nonprotected_still_admin():
    # write-in-read is admin regardless of target table
    assert _role(
        "WITH t AS (DELETE FROM res_partner RETURNING id) SELECT * FROM t"
    ) == "admin"


def test_comment_prefixed_cte_denied():
    assert _role(
        "/*x*/ WITH t AS (DELETE FROM res_users RETURNING id) SELECT * FROM t"
    ) == "admin"


def test_multi_statement_denied():
    assert _role("SELECT 1; DELETE FROM res_users") == "admin"


def test_select_into_denied():
    assert _role("SELECT * INTO new_tbl FROM res_users") == "admin"


# ── regressions: plain reads still allowed ──
def test_plain_select_user():
    assert _role("SELECT id FROM res_partner LIMIT 5") == "user"


def test_join_select_user():
    assert _role(
        "SELECT p.id FROM res_partner p JOIN res_users u ON u.partner_id=p.id"
    ) == "user"


def test_plain_insert_returning_nonprotected_user():
    assert _role("INSERT INTO mail_message(body) VALUES('x') RETURNING id") == "user"


# ── fail-closed ──
def test_parse_error_admin():
    assert _role("SELEKT bork FROM (((") == "admin"


def test_empty_admin():
    assert C.classify_sql("")["role_required"] == "admin"


# ── admin bypass ──
def test_admin_bypasses_all():
    allowed, _ = C.is_allowed_for_role(
        "WITH t AS (DELETE FROM res_users RETURNING id) SELECT * FROM t", "admin")
    assert allowed is True
