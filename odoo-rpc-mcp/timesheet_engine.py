"""
Timesheet engine — parse memory progress files + propose/create Odoo timesheet entries.

Workflow:
  1. Scan memory files modified within a date range (default = current week).
  2. For each file, infer the project via:
       a. explicit `project_id:` frontmatter (highest priority)
       b. filename keyword match (`project_<keyword>_*.md`) against res.partner / project.project names
       c. content keyword scan against a configurable keyword→project map
  3. Estimate hours per (date, project) from memory content density + timestamp markers.
  4. Compare with already-logged `account.analytic.line` entries (avoid duplicates).
  5. Return a preview report OR create the missing entries (when `dry_run=False`).

The engine is intentionally conservative: it never overwrites existing entries and
only proposes additions. Re-running on the same week is idempotent.

Used by:
  - tool `odoo_timesheet_from_memory` (proposes + optionally creates)
  - tool `odoo_timesheet_weekly_report` (read-only week summary)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# Memory directory layout matches what server.py exposes via `_memory_*_dir`.
DEFAULT_MEMORY_DIR = os.environ.get("MEMORY_DIR", "/data/memory")

# Filename prefixes we treat as progress entries (vs. reference / feedback).
PROGRESS_PREFIXES = ("project_", "session_", "qa_plan_", "roadmap_")

# Frontmatter key that lets a memory file pin itself to a specific Odoo project.
PROJECT_KEY = "project_id"          # numeric Odoo project.project id
PROJECT_NAME_KEY = "project_name"   # alternative — matched fuzzy against project.project.name


# ───────────────────────────── Data classes ─────────────────────────────


@dataclass
class MemoryEntry:
    """One memory file relevant to a timesheet week."""
    path: str
    name: str                # filename without ext
    modified: datetime
    body: str                # full markdown content
    frontmatter: dict        # parsed YAML-ish frontmatter
    referenced_dates: list[date] = field(default_factory=list)


@dataclass
class TimesheetProposal:
    """A proposed account.analytic.line entry not yet in Odoo."""
    date: str                # YYYY-MM-DD
    project_id: int
    project_name: str
    task_id: int | None
    hours: float
    description: str
    source_files: list[str] = field(default_factory=list)
    confidence: str = "medium"   # high / medium / low


# ───────────────────────────── Memory scanning ─────────────────────────────


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Strip leading YAML frontmatter (--- ... ---) and return (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    meta: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


# Loose ISO-style date match: YYYY-MM-DD anywhere in the body. Used to
# attribute work to a day even when the file itself was edited later.
_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


def _extract_referenced_dates(body: str) -> list[date]:
    """Pull out YYYY-MM-DD literals from the markdown body."""
    out: list[date] = []
    seen: set[str] = set()
    for m in _DATE_RE.finditer(body):
        s = m.group(0)
        if s in seen:
            continue
        seen.add(s)
        try:
            out.append(date.fromisoformat(s))
        except ValueError:
            pass
    return out


def scan_memory(
    memory_dir: str,
    week_start: date,
    week_end: date,
    prefixes: tuple[str, ...] = PROGRESS_PREFIXES,
) -> list[MemoryEntry]:
    """Return memory entries either modified in the week, or with a date inside the week."""
    entries: list[MemoryEntry] = []
    if not os.path.isdir(memory_dir):
        return entries
    # Walk shared + user dirs (server.py owns the per-user dispatch; we accept any dir tree).
    for root, _dirs, files in os.walk(memory_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            stem = fname[:-3]
            if not any(stem.startswith(p) for p in prefixes):
                continue
            fpath = os.path.join(root, fname)
            try:
                stat = os.stat(fpath)
                text = Path(fpath).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            modified = datetime.fromtimestamp(stat.st_mtime)
            meta, body = _parse_frontmatter(text)
            ref_dates = _extract_referenced_dates(body)
            in_window = (
                week_start <= modified.date() <= week_end
                or any(week_start <= d <= week_end for d in ref_dates)
            )
            if not in_window:
                continue
            entries.append(MemoryEntry(
                path=fpath,
                name=stem,
                modified=modified,
                body=body,
                frontmatter=meta,
                referenced_dates=[d for d in ref_dates if week_start <= d <= week_end],
            ))
    return entries


# ───────────────────────── Project resolution ─────────────────────────


# Built-in keyword → fuzzy project-name fragment. The actual project_id is
# resolved at runtime against `project.project.search_read`.
DEFAULT_KEYWORD_MAP: dict[str, str] = {
    "alpinter":   "Алпинтер",
    "teolino":    "Теолино",
    "теолино":    "Теолино",
    "tri_wall":   "Tri-Wall",
    "tri-wall":   "Tri-Wall",
    "triwall":    "Tri-Wall",
    "poligroup":  "Полигруп",
    "полигруп":   "Полигруп",
    "secdoor":    "SECDOOR",
    "natural_heroes": "NATURAL HEROES",
    "nektar":     "Nektar Natura",  # may be CRM lead, not project — caller decides
    "solid_55":   "СОЛИД 55",
    "solid55":    "СОЛИД 55",
    "design_matrix": "MRP Design Matrix",
    "mrp_design": "MRP Design Matrix",
}


def build_project_index(connection, keyword_map: dict[str, str] | None = None) -> dict[str, dict]:
    """Resolve keyword fragments to actual Odoo projects.

    Returns: { keyword: { 'id': int, 'name': str, 'partner_id': [..], 'task_ids': [...] } }
    """
    keyword_map = dict(keyword_map or DEFAULT_KEYWORD_MAP)
    name_fragments = sorted({frag for frag in keyword_map.values()})
    index: dict[str, dict] = {}
    for frag in name_fragments:
        rows = connection.execute_kw(
            "project.project", "search_read",
            [[["name", "ilike", frag]]],
            {"fields": ["id", "name", "partner_id"], "limit": 5},
        )
        if not rows:
            continue
        # Pick the active production project (lowest id heuristic — earliest created).
        rows.sort(key=lambda r: r["id"])
        chosen = rows[0]
        for kw, target in keyword_map.items():
            if target == frag:
                index[kw] = chosen
    return index


def resolve_project_for_entry(
    entry: MemoryEntry,
    project_index: dict[str, dict],
) -> dict | None:
    """Return chosen project dict or None.

    Priority:
      1. frontmatter project_id (numeric)
      2. frontmatter project_name (fuzzy match against project_index)
      3. filename keyword scan
      4. body keyword scan (lowercased, first match wins, ranked by frequency)
    """
    # 1) explicit numeric pin
    pin = entry.frontmatter.get(PROJECT_KEY)
    if pin and str(pin).isdigit():
        return {"id": int(pin), "name": entry.frontmatter.get(PROJECT_NAME_KEY, f"P{pin}"),
                "partner_id": False, "_resolved_via": "frontmatter:project_id"}
    # 2) fuzzy name pin
    name_pin = entry.frontmatter.get(PROJECT_NAME_KEY, "").strip().lower()
    if name_pin:
        for proj in project_index.values():
            if name_pin in proj["name"].lower():
                return {**proj, "_resolved_via": "frontmatter:project_name"}
    # 3) filename keyword match
    fname_low = entry.name.lower()
    for kw, proj in project_index.items():
        if kw in fname_low:
            return {**proj, "_resolved_via": f"filename:{kw}"}
    # 4) body keyword scan with frequency ranking
    body_low = entry.body.lower()
    hits: list[tuple[str, int]] = []
    for kw in project_index:
        n = body_low.count(kw)
        if n:
            hits.append((kw, n))
    if hits:
        hits.sort(key=lambda x: -x[1])
        kw = hits[0][0]
        return {**project_index[kw], "_resolved_via": f"body:{kw}(x{hits[0][1]})"}
    return None


# ───────────────────────── Hour estimation ─────────────────────────


def estimate_hours(entry: MemoryEntry, day: date) -> float:
    """Heuristic hour estimate per memory entry per day.

    Rules of thumb (calibrated for terse Bulgarian dev notes):
      - File modified that day   → base 1.0h
      - Body length > 4000 chars → +0.5h
      - Body length > 8000 chars → +1.0h (instead of +0.5)
      - Body mentions explicit time markers (`Hh`, `Hч`) → use those
    Capped at 4.0h per file per day.
    """
    # Look for explicit hour markers in body assigned to this day.
    day_str = day.isoformat()
    if day_str in entry.body:
        # Find a slice around the date and look for "Xh" / "X.Xч" within ~200 chars.
        idx = entry.body.find(day_str)
        slice_ = entry.body[max(0, idx - 50): idx + 250]
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|ч|hours|часа?)", slice_, re.IGNORECASE)
        if m:
            try:
                explicit = float(m.group(1))
                if 0.25 <= explicit <= 8:
                    return explicit
            except ValueError:
                pass
    # Default: density-based estimate.
    base = 1.0 if entry.modified.date() == day else 0.5
    if len(entry.body) > 8000:
        base += 1.0
    elif len(entry.body) > 4000:
        base += 0.5
    return min(base, 4.0)


# ───────────────────────── Existing entries lookup ─────────────────────────


def fetch_existing_lines(
    connection,
    employee_id: int,
    week_start: date,
    week_end: date,
) -> dict[tuple[str, int], float]:
    """Return { (date_str, project_id): hours_sum } for entries already in Odoo."""
    lines = connection.execute_kw(
        "account.analytic.line", "search_read",
        [[
            ["employee_id", "=", employee_id],
            ["date", ">=", week_start.isoformat()],
            ["date", "<=", week_end.isoformat()],
            ["project_id", "!=", False],
        ]],
        {"fields": ["date", "project_id", "unit_amount"], "limit": 500},
    )
    out: dict[tuple[str, int], float] = {}
    for ln in lines:
        if not ln.get("project_id"):
            continue
        key = (ln["date"], ln["project_id"][0])
        out[key] = out.get(key, 0.0) + (ln.get("unit_amount") or 0.0)
    return out


# ───────────────────────── Top-level engine API ─────────────────────────


def week_bounds(week_start: str | None) -> tuple[date, date]:
    """Resolve a week's Monday/Sunday from optional ISO date string."""
    if week_start:
        start = date.fromisoformat(week_start)
    else:
        today = date.today()
        start = today - timedelta(days=today.weekday())  # Monday of current week
    end = start + timedelta(days=6)
    return start, end


def build_proposals(
    connection,
    employee_id: int,
    week_start: date,
    week_end: date,
    memory_dir: str = DEFAULT_MEMORY_DIR,
    keyword_map: dict[str, str] | None = None,
) -> tuple[list[TimesheetProposal], list[dict]]:
    """Generate proposals for missing entries + a list of unresolved memory files.

    Returns (proposals, unresolved).
    """
    entries = scan_memory(memory_dir, week_start, week_end)
    proj_index = build_project_index(connection, keyword_map)
    existing = fetch_existing_lines(connection, employee_id, week_start, week_end)

    # bucket: (date_str, project_id) → list[(entry, hours, description)]
    buckets: dict[tuple[str, int], list[tuple[MemoryEntry, float, str]]] = {}
    unresolved: list[dict] = []

    for entry in entries:
        proj = resolve_project_for_entry(entry, proj_index)
        if not proj:
            unresolved.append({"file": entry.name, "modified": entry.modified.isoformat()})
            continue
        # Days the entry contributes to: referenced dates within week, plus modified-day if in week.
        target_days: set[date] = set(entry.referenced_dates)
        if week_start <= entry.modified.date() <= week_end:
            target_days.add(entry.modified.date())
        for day in sorted(target_days):
            hours = estimate_hours(entry, day)
            short_desc = entry.name.replace("_", " ")
            buckets.setdefault((day.isoformat(), proj["id"]), []).append(
                (entry, hours, short_desc)
            )

    # Convert buckets → proposals, subtracting any hours already present.
    proposals: list[TimesheetProposal] = []
    for (day_str, proj_id), items in sorted(buckets.items()):
        proj_name = items[0][0].frontmatter.get(PROJECT_NAME_KEY) or _name_for(proj_id, items[0][0], proj_index)
        total = sum(h for _, h, _ in items)
        already = existing.get((day_str, proj_id), 0.0)
        delta = round(total - already, 2)
        if delta <= 0:
            continue
        descriptions = sorted({d for _, _, d in items})
        sources = [e.name for e, _, _ in items]
        proposals.append(TimesheetProposal(
            date=day_str,
            project_id=proj_id,
            project_name=proj_name,
            task_id=None,  # caller may pre-pick a task; engine stays neutral
            hours=delta,
            description=" / ".join(descriptions[:3]) + (" + …" if len(descriptions) > 3 else ""),
            source_files=sources,
            confidence="high" if any("frontmatter" in (resolve_project_for_entry(e, proj_index) or {}).get("_resolved_via", "") for e, _, _ in items) else "medium",
        ))
    return proposals, unresolved


def _name_for(project_id: int, entry: MemoryEntry, idx: dict[str, dict]) -> str:
    for proj in idx.values():
        if proj["id"] == project_id:
            return proj["name"]
    return f"Project #{project_id}"


def create_entries(
    connection,
    employee_id: int,
    proposals: list[TimesheetProposal],
) -> list[dict]:
    """Create account.analytic.line records. Returns [{proposal, line_id, status}]."""
    results: list[dict] = []
    for p in proposals:
        vals = {
            "date": p.date,
            "employee_id": employee_id,
            "project_id": p.project_id,
            "unit_amount": p.hours,
            "name": p.description[:255],
        }
        if p.task_id:
            vals["task_id"] = p.task_id
        try:
            new_id = connection.execute_kw(
                "account.analytic.line", "create", [vals]
            )
            results.append({"proposal": asdict(p), "line_id": new_id, "status": "created"})
        except Exception as e:
            results.append({"proposal": asdict(p), "line_id": None, "status": "error", "error": str(e)})
    return results


def employee_for_user(connection, user_id: int | None = None) -> int | None:
    """Resolve hr.employee.id linked to a res.users.id (defaults to current user)."""
    if user_id is None:
        user_id = connection.authenticate()
    rows = connection.execute_kw(
        "hr.employee", "search_read",
        [[["user_id", "=", user_id]]],
        {"fields": ["id"], "limit": 1},
    )
    return rows[0]["id"] if rows else None
