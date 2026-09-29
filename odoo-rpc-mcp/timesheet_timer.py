"""
Нативният таймер на таймшийтите в Odoo Enterprise (`timer.timer`), управляван през RPC.

Решението и защо е такова: specs/mcp-timesheet-timer/adr/0001.

Накратко:
  - таймерът живее САМО в Odoo (`timer.timer`), MCP не пази състояние;
  - старт винаги върху ред (`account.analytic.line`) — еднакъв път в 18 и 19;
  - стоп без confirm е предложение (сървърен часовник + нативното закръгляне);
  - методи, които връщат None/recordset, гърмят по XML-RPC СЛЕД комита —
    `_call_void` приема само тази грешка и след нея сверяваме състоянието.

Ползва се от tool `odoo_timesheet_timer` в server.py.
"""

from __future__ import annotations

from datetime import datetime

LINE = "account.analytic.line"
TASK = "project.task"
TIMER = "timer.timer"
TASK_WIZARD = "project.task.create.timesheet"

ACTIONS = ("start", "status", "stop", "cancel")

_DT_FORMAT = "%Y-%m-%d %H:%M:%S"


class TimerError(Exception):
    """Грешка, която се връща на модела като {"error": ...}."""


# ───────────────────────── RPC помощници ─────────────────────────


def _call_void(conn, model: str, method: str, args: list, kwargs: dict | None = None):
    """Вика метод, чийто резултат може да не минава през XML-RPC.

    Връща резултата или None, ако сървърът е изпълнил метода, но не е могъл да
    сериализира отговора („cannot marshal“). Всяка друга грешка минава нагоре.
    Извикващият ДЛЪЖИ да свери състоянието след това.
    """
    try:
        return conn.execute_kw(model, method, args, kwargs or {})
    except Exception as exc:  # execute_kw обвива Fault в Exception
        if "cannot marshal" in str(exc):
            return None
        raise


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    # xmlrpc.client.DateTime, str или datetime — всички се свеждат до текст
    text = value if isinstance(value, str) else str(value)
    text = text.replace("T", " ").split(".")[0]
    for fmt in (_DT_FORMAT, "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise TimerError(f"unexpected datetime from server: {value!r}")


def _m2o(value):
    """[id, name] → {"id", "name"}; False → None."""
    if not value:
        return None
    return {"id": value[0], "name": value[1]}


# ───────────────────────── Откриване и четене ─────────────────────────


def ensure_available(conn) -> None:
    """Таймерът е наличен, ако редът от таймшийта носи `timer_start`.

    По признак, не по версия: Community и Odoo 20 нямат полето на реда.
    """
    fields = conn.execute_kw(LINE, "fields_get", [], {"attributes": ["type"]})
    if "timer_start" not in fields:
        raise TimerError(
            "The native timesheet timer is not available on this database "
            "(needs Enterprise 'timesheet_grid' on Odoo 17-19; Community and "
            "Odoo 20 have no timer on timesheet lines)."
        )


def server_now(conn) -> datetime:
    return _parse_dt(conn.execute_kw(TIMER, "get_server_time", [], {}))


def elapsed_minutes(timer: dict, now: datetime) -> float:
    """Същото като `timer.timer._get_minutes_spent`: паузата се изважда."""
    start = _parse_dt(timer.get("timer_start"))
    if not start:
        return 0.0
    pause = _parse_dt(timer.get("timer_pause"))
    end = pause if pause else now
    return max((end - start).total_seconds() / 60.0, 0.0)


def rounded_hours(conn, minutes: float) -> float:
    """Нативното закръгляне (timesheet_min_duration / timesheet_rounding)."""
    return conn.execute_kw(LINE, "get_rounded_time", [minutes], {})


def list_timers(conn, uid: int) -> list[dict]:
    """Таймерите на потребителя (течащи и на пауза), обогатени с реда/задачата."""
    timers = conn.execute_kw(
        TIMER, "search_read",
        [[["user_id", "=", uid], ["timer_start", "!=", False]]],
        {"fields": ["res_model", "res_id", "timer_start", "timer_pause"]},
    )
    line_ids = [t["res_id"] for t in timers if t["res_model"] == LINE]
    task_ids = [t["res_id"] for t in timers if t["res_model"] == TASK]
    lines = {}
    if line_ids:
        for ln in conn.execute_kw(
            LINE, "read", [line_ids],
            {"fields": ["name", "date", "project_id", "task_id", "unit_amount"]},
        ):
            lines[ln["id"]] = ln
    tasks = {}
    if task_ids:
        for tk in conn.execute_kw(
            TASK, "read", [task_ids], {"fields": ["name", "project_id"]},
        ):
            tasks[tk["id"]] = tk

    out = []
    for t in timers:
        item = {
            "timer_id": t["id"],
            "res_model": t["res_model"],
            "res_id": t["res_id"],
            "timer_start": t["timer_start"],
            "running": not t.get("timer_pause"),
            "_raw": t,
        }
        if t["res_model"] == LINE and t["res_id"] in lines:
            ln = lines[t["res_id"]]
            item.update({
                "timesheet_id": ln["id"],
                "description": "" if ln["name"] == "/" else ln["name"],
                "date": ln["date"],
                "project": _m2o(ln["project_id"]),
                "task": _m2o(ln["task_id"]),
                "logged_hours": ln["unit_amount"],
            })
        elif t["res_model"] == TASK and t["res_id"] in tasks:
            tk = tasks[t["res_id"]]
            item.update({
                "task": {"id": tk["id"], "name": tk["name"]},
                "project": _m2o(tk["project_id"]),
            })
        out.append(item)
    return out


def _with_elapsed(conn, timers: list[dict], now: datetime) -> list[dict]:
    for item in timers:
        minutes = elapsed_minutes(item.pop("_raw"), now)
        item["elapsed_minutes"] = round(minutes, 1)
        item["rounded_hours"] = rounded_hours(conn, minutes)
    return timers


def _pick_target(timers: list[dict], timesheet_id: int | None, task_id: int | None) -> dict:
    """Кой таймер спираме/отказваме: изрично посочен, иначе единственият (течащият с предимство)."""
    if timesheet_id:
        found = [t for t in timers if t["res_model"] == LINE and t["res_id"] == timesheet_id]
        if not found:
            raise TimerError(f"No timer of yours runs on timesheet line {timesheet_id}.")
        return found[0]
    if task_id:
        found = [
            t for t in timers
            if (t["res_model"] == TASK and t["res_id"] == task_id)
            or (t.get("task") or {}).get("id") == task_id
        ]
        if len(found) == 1:
            return found[0]
        if not found:
            raise TimerError(f"No timer of yours runs on task {task_id}.")
        raise TimerError(f"Several timers on task {task_id}; pass timesheet_id.")
    if not timers:
        raise TimerError("You have no timer running.")
    running = [t for t in timers if t["running"]]
    if len(running) == 1:
        return running[0]
    if len(timers) == 1:
        return timers[0]
    raise TimerError(
        "Several timers (running or paused); pass timesheet_id or task_id. "
        "Use action='status' to list them."
    )


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


# ───────────────────────── Действия ─────────────────────────


def status(conn, uid: int) -> dict:
    ensure_available(conn)
    now = server_now(conn)
    timers = _with_elapsed(conn, list_timers(conn, uid), now)
    return {
        "server_time": now.strftime(_DT_FORMAT),
        "timers": [_public(t) for t in timers],
        "running_count": sum(1 for t in timers if t["running"]),
    }


def start(
    conn, uid: int, *,
    task_id: int | None = None,
    project_id: int | None = None,
    description: str = "",
    confirm: bool = False,
) -> dict:
    ensure_available(conn)
    if not task_id and not project_id:
        raise TimerError("Pass task_id or project_id.")
    if task_id:
        task = conn.execute_kw(TASK, "read", [[task_id]], {"fields": ["project_id", "name"]})
        if not task:
            raise TimerError(f"Task {task_id} not found.")
        if not task[0]["project_id"]:
            raise TimerError(f"Task {task_id} has no project; timesheets need one.")
        project_id = task[0]["project_id"][0]

    before = list_timers(conn, uid)
    running = [t for t in before if t["running"]]
    if running and not confirm:
        now = server_now(conn)
        return {
            "needs_confirm": True,
            "message": (
                "A timer is already running. Starting a new one lets Odoo stop it "
                "(timesheet line: time is logged) or pause it (Odoo 18 task timer). "
                "Repeat with confirm=true to proceed."
            ),
            "running": [_public(t) for t in _with_elapsed(conn, running, now)],
        }

    vals = {"project_id": project_id, "name": description or "/"}
    if task_id:
        vals["task_id"] = task_id
    line_id = conn.execute_kw(LINE, "create", [vals], {})
    result = _call_void(conn, LINE, "action_timer_start", [[line_id]])
    # 19 връща данните на реда; може да е НОВ ред (друга дата / валидиран период)
    started_id = result.get("id", line_id) if isinstance(result, dict) else line_id

    after = list_timers(conn, uid)
    mine = [
        t for t in after
        if t["res_model"] == LINE and t["res_id"] in (line_id, started_id) and t["running"]
    ]
    if not mine:
        # таймерът не тръгна (напр. проектът не позволява таймшийт) — чистим празния ред
        leftover = conn.execute_kw(LINE, "read", [[line_id]], {"fields": ["unit_amount"]})
        if leftover and not leftover[0]["unit_amount"]:
            conn.execute_kw(LINE, "unlink", [[line_id]], {})
        raise TimerError(
            "Odoo did not start the timer (project/task may not allow timesheets "
            "or the timesheet timer is disabled)."
        )
    # прекъснати = течаха преди, вече не текат (спрени с запис или на пауза)
    still = {t["timer_id"] for t in after if t["running"]}
    return {
        "started": _public(mine[0]),
        "interrupted": [_public(t) for t in running if t["timer_id"] not in still],
    }


def stop(
    conn, uid: int, *,
    timesheet_id: int | None = None,
    task_id: int | None = None,
    description: str | None = None,
    confirm: bool = False,
) -> dict:
    ensure_available(conn)
    now = server_now(conn)
    timers = _with_elapsed(conn, list_timers(conn, uid), now)
    target = _pick_target(timers, timesheet_id, task_id)

    proposal = {
        "timer": _public(target),
        "hours_to_log": target["rounded_hours"],
        "description": description if description is not None else target.get("description", ""),
    }
    if not confirm:
        return {
            "proposal": proposal,
            "message": "Nothing written. Repeat with confirm=true to log this time.",
        }

    if target["res_model"] == LINE:
        line_id = target["res_id"]
        added = conn.execute_kw(LINE, "action_timer_stop", [[line_id]], {})
        if description:
            conn.execute_kw(LINE, "write", [[line_id], {"name": description}], {})
        line = conn.execute_kw(
            LINE, "read", [[line_id]],
            {"fields": ["name", "date", "project_id", "task_id", "unit_amount"]},
        )
        return {
            "logged": {
                "timesheet_id": line_id,
                "added_hours": added,
                "total_hours": line[0]["unit_amount"] if line else None,
                "description": line[0]["name"] if line else description,
                "date": line[0]["date"] if line else None,
            },
        }

    if target["res_model"] == TASK:
        # Odoo 18: таймер върху задачата — нативният визард, в една транзакция
        hours = target["rounded_hours"]
        if hours <= 0:
            raise TimerError("Rounded time is 0; use action='cancel' to drop the timer.")
        wiz_id = conn.execute_kw(
            TASK_WIZARD, "create",
            [{"task_id": target["res_id"], "time_spent": hours,
              "description": proposal["description"] or "/"}],
            {"context": {"active_id": target["res_id"], "active_model": TASK}},
        )
        _call_void(conn, TASK_WIZARD, "save_timesheet", [[wiz_id]])
        left = [t for t in list_timers(conn, uid) if t["timer_id"] == target["timer_id"]]
        if left:
            raise TimerError("Odoo did not close the task timer; check the task in Odoo.")
        return {"logged": {"task_id": target["res_id"], "added_hours": hours,
                           "description": proposal["description"]}}

    raise TimerError(
        f"Timer on {target['res_model']} is not a timesheet timer; stop it in Odoo."
    )


def cancel(
    conn, uid: int, *,
    timesheet_id: int | None = None,
    task_id: int | None = None,
    confirm: bool = False,
) -> dict:
    """Същото като `action_timer_unlink`, но на ниво записи (методът връща None)."""
    ensure_available(conn)
    now = server_now(conn)
    timers = _with_elapsed(conn, list_timers(conn, uid), now)
    target = _pick_target(timers, timesheet_id, task_id)
    if not confirm:
        return {
            "proposal": {"discard": _public(target)},
            "message": "Nothing deleted. Repeat with confirm=true to discard this timer "
                       "without logging time.",
        }
    conn.execute_kw(TIMER, "unlink", [[target["timer_id"]]], {})
    removed_line = False
    if target["res_model"] == LINE and not target.get("logged_hours"):
        conn.execute_kw(LINE, "unlink", [[target["res_id"]]], {})
        removed_line = True
    return {"cancelled": _public(target), "removed_empty_line": removed_line}


def run(conn, action: str, args: dict) -> dict:
    """Входна точка за tool-а. Връща dict; TimerError → {"error": ...}."""
    if action not in ACTIONS:
        return {"error": f"action must be one of {', '.join(ACTIONS)}"}
    try:
        uid = conn.authenticate()
        if action == "status":
            return status(conn, uid)
        if action == "start":
            return start(
                conn, uid,
                task_id=args.get("task_id"),
                project_id=args.get("project_id"),
                description=args.get("description") or "",
                confirm=bool(args.get("confirm")),
            )
        if action == "stop":
            return stop(
                conn, uid,
                timesheet_id=args.get("timesheet_id"),
                task_id=args.get("task_id"),
                description=args.get("description"),
                confirm=bool(args.get("confirm")),
            )
        return cancel(
            conn, uid,
            timesheet_id=args.get("timesheet_id"),
            task_id=args.get("task_id"),
            confirm=bool(args.get("confirm")),
        )
    except TimerError as exc:
        return {"error": str(exc)}
