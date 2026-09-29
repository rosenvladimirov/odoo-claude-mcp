"""Тестове за timesheet_timer.py — нативният таймер на Odoo през RPC.

FakeOdoo имитира поведението от сорсовете на EE 18/19 (timer, timesheet_grid):
  - 19: action_timer_start на реда връща dict; 18: връща None ⇒ „cannot marshal“
    СЛЕД като записът е станал;
  - старт спира течащия таймер на ред (записва време) / паузира таймер на задача (18);
  - get_rounded_time: минимум 15 мин, закръгляне нагоре през 15 мин.

Пускане от корена на odoo-rpc-mcp:
    pytest tests/test_timesheet_timer.py -v
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import timesheet_timer as tt  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"
UID = 7


class FakeOdoo:
    def __init__(self, version=19, has_timer=True):
        self.version = version
        self.has_timer = has_timer
        self.now = datetime(2026, 9, 29, 10, 0, 0)
        self.lines = {}
        self.timers = {}
        self.tasks = {
            11: {"id": 11, "name": "Технически задачи", "project_id": [402, "Теолино"]},
            12: {"id": 12, "name": "Без проект", "project_id": False},
        }
        self.wizards = {}
        self._seq = 100
        self.calls = []

    # ── помощници ──
    def _id(self):
        self._seq += 1
        return self._seq

    def _timer_for(self, model, res_id):
        for t in self.timers.values():
            if t["res_model"] == model and t["res_id"] == res_id and t["user_id"] == UID:
                return t
        return None

    def _minutes(self, t):
        start = datetime.strptime(t["timer_start"], FMT)
        end = datetime.strptime(t["timer_pause"], FMT) if t["timer_pause"] else self.now
        return (end - start).total_seconds() / 60

    @staticmethod
    def _round(minutes):
        return max(15, math.ceil(minutes / 15) * 15) / 60

    def _stop_line(self, line_id):
        t = self._timer_for(tt.LINE, line_id)
        if not t:
            return 0
        hours = self._round(self._minutes(t))
        self.lines[line_id]["unit_amount"] += hours
        del self.timers[t["id"]]
        return hours

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)

    def authenticate(self):
        return UID

    # ── RPC ──
    def execute_kw(self, model, method, args, kwargs):
        self.calls.append((model, method))
        if model == tt.LINE and method == "fields_get":
            f = {"name": {}, "unit_amount": {}}
            if self.has_timer:
                f["timer_start"] = {}
            return f
        if model == tt.TIMER and method == "get_server_time":
            return self.now.strftime(FMT)
        if model == tt.LINE and method == "get_rounded_time":
            return self._round(args[0])
        if model == tt.TIMER and method == "search_read":
            return [dict(t) for t in self.timers.values()
                    if t["user_id"] == UID and t["timer_start"]]
        if model == tt.TIMER and method == "unlink":
            for i in args[0]:
                self.timers.pop(i, None)
            return True
        if model == tt.TASK and method == "read":
            return [dict(self.tasks[i]) for i in args[0] if i in self.tasks]
        if model == tt.LINE and method == "read":
            return [dict(self.lines[i]) for i in args[0] if i in self.lines]
        if model == tt.LINE and method == "create":
            v = args[0]
            lid = self._id()
            task = self.tasks.get(v.get("task_id"))
            self.lines[lid] = {
                "id": lid, "name": v.get("name", "/"), "date": "2026-09-29",
                "project_id": [v["project_id"], "P"],
                "task_id": [task["id"], task["name"]] if task else False,
                "unit_amount": v.get("unit_amount", 0.0),
            }
            return lid
        if model == tt.LINE and method == "write":
            for i in args[0]:
                self.lines[i].update(args[1])
            return True
        if model == tt.LINE and method == "unlink":
            for i in args[0]:
                self.lines.pop(i)
            return True
        if model == tt.LINE and method == "action_timer_start":
            lid = args[0][0]
            # нативното: течащият таймер се прекъсва
            for t in list(self.timers.values()):
                if t["user_id"] == UID and t["timer_start"] and not t["timer_pause"]:
                    if t["res_model"] == tt.LINE:
                        self._stop_line(t["res_id"])
                    else:  # задача в 18 → пауза
                        t["timer_pause"] = self.now.strftime(FMT)
            tid = self._id()
            self.timers[tid] = {
                "id": tid, "res_model": tt.LINE, "res_id": lid, "user_id": UID,
                "timer_start": self.now.strftime(FMT), "timer_pause": False,
            }
            if self.version == 18:
                raise Exception("Odoo RPC fault: cannot marshal None unless allow_none is enabled")
            return {"id": lid, "name": ""}
        if model == tt.LINE and method == "action_timer_stop":
            return self._stop_line(args[0][0])
        if model == tt.TASK_WIZARD and method == "create":
            wid = self._id()
            self.wizards[wid] = dict(args[0])
            return wid
        if model == tt.TASK_WIZARD and method == "save_timesheet":
            w = self.wizards[args[0][0]]
            t = self._timer_for(tt.TASK, w["task_id"])
            del self.timers[t["id"]]
            lid = self.execute_kw(tt.LINE, "create", [{
                "project_id": 402, "task_id": w["task_id"],
                "name": w["description"], "unit_amount": w["time_spent"]}], {})
            self.last_wizard_line = lid
            raise Exception("Odoo RPC fault: cannot marshal <class 'odoo.api.account.analytic.line'> objects")
        raise AssertionError(f"unexpected call {model}.{method}")

    def add_task_timer(self, task_id, minutes_ago):
        tid = self._id()
        self.timers[tid] = {
            "id": tid, "res_model": tt.TASK, "res_id": task_id, "user_id": UID,
            "timer_start": (self.now - timedelta(minutes=minutes_ago)).strftime(FMT),
            "timer_pause": False,
        }
        return tid


@pytest.fixture(params=[18, 19])
def odoo(request):
    return FakeOdoo(version=request.param)


def test_not_available_on_community():
    res = tt.run(FakeOdoo(has_timer=False), "status", {})
    assert "not available" in res["error"]


def test_unknown_action():
    assert "action must be" in tt.run(FakeOdoo(), "pause", {})["error"]


def test_start_stop_full_cycle(odoo):
    res = tt.run(odoo, "start", {"task_id": 11, "description": "деплой"})
    assert "error" not in res, res
    line_id = res["started"]["timesheet_id"]
    assert res["started"]["task"]["id"] == 11

    odoo.advance(50)
    st = tt.run(odoo, "status", {})
    assert st["running_count"] == 1
    assert st["timers"][0]["elapsed_minutes"] == 50.0
    assert st["timers"][0]["rounded_hours"] == 1.0

    # без confirm — нищо не се пише
    prop = tt.run(odoo, "stop", {})
    assert prop["proposal"]["hours_to_log"] == 1.0
    assert odoo.lines[line_id]["unit_amount"] == 0.0
    assert odoo.timers, "timer must still run after a proposal"

    done = tt.run(odoo, "stop", {"confirm": True, "description": "деплой на Конекс"})
    assert done["logged"]["added_hours"] == 1.0
    assert odoo.lines[line_id]["unit_amount"] == 1.0
    assert odoo.lines[line_id]["name"] == "деплой на Конекс"
    assert not odoo.timers


def test_start_project_without_task(odoo):
    res = tt.run(odoo, "start", {"project_id": 402})
    assert odoo.lines[res["started"]["timesheet_id"]]["task_id"] is False


def test_start_needs_task_or_project(odoo):
    assert "task_id or project_id" in tt.run(odoo, "start", {})["error"]


def test_start_task_without_project(odoo):
    assert "no project" in tt.run(odoo, "start", {"task_id": 12})["error"]


def test_start_while_running_requires_confirm(odoo):
    first = tt.run(odoo, "start", {"task_id": 11})["started"]["timesheet_id"]
    odoo.advance(20)
    res = tt.run(odoo, "start", {"project_id": 402})
    assert res["needs_confirm"] is True
    assert len(odoo.lines) == 1, "no second line may be created without confirm"
    assert odoo.lines[first]["unit_amount"] == 0.0

    res = tt.run(odoo, "start", {"project_id": 402, "confirm": True})
    assert [i["timesheet_id"] for i in res["interrupted"]] == [first]
    # нативното прекъсване е записало времето на първия ред
    assert odoo.lines[first]["unit_amount"] == 0.5


def test_cancel_removes_empty_line(odoo):
    line_id = tt.run(odoo, "start", {"task_id": 11})["started"]["timesheet_id"]
    prop = tt.run(odoo, "cancel", {})
    assert "proposal" in prop and odoo.timers
    res = tt.run(odoo, "cancel", {"confirm": True})
    assert res["removed_empty_line"] is True
    assert not odoo.timers and line_id not in odoo.lines


def test_stop_without_timer(odoo):
    assert "no timer" in tt.run(odoo, "stop", {})["error"]


def test_stop_by_unknown_timesheet(odoo):
    tt.run(odoo, "start", {"task_id": 11})
    assert "999" in tt.run(odoo, "stop", {"timesheet_id": 999})["error"]


def test_start_failure_cleans_up_empty_line():
    odoo = FakeOdoo()
    orig = odoo.execute_kw

    def no_timer(model, method, args, kwargs):
        if method == "action_timer_start":
            return {}  # Odoo не стартира (display_timer = False)
        return orig(model, method, args, kwargs)

    odoo.execute_kw = no_timer
    res = tt.run(odoo, "start", {"task_id": 11})
    assert "did not start" in res["error"]
    assert not odoo.lines


def test_other_rpc_errors_propagate():
    odoo = FakeOdoo()
    orig = odoo.execute_kw

    def boom(model, method, args, kwargs):
        if method == "action_timer_start":
            raise Exception("Odoo RPC fault: You cannot use the timer on validated timesheets.")
        return orig(model, method, args, kwargs)

    odoo.execute_kw = boom
    with pytest.raises(Exception, match="validated"):
        tt.run(odoo, "start", {"task_id": 11})


def test_odoo18_task_timer_stop_via_wizard():
    odoo = FakeOdoo(version=18)
    odoo.add_task_timer(11, minutes_ago=100)
    st = tt.run(odoo, "status", {})
    assert st["timers"][0]["res_model"] == tt.TASK
    assert st["timers"][0]["rounded_hours"] == 1.75

    res = tt.run(odoo, "stop", {"task_id": 11, "confirm": True, "description": "среща"})
    assert res["logged"]["added_hours"] == 1.75
    assert not odoo.timers
    line = odoo.lines[odoo.last_wizard_line]
    assert line["unit_amount"] == 1.75 and line["name"] == "среща"


def test_odoo18_start_pauses_task_timer():
    odoo = FakeOdoo(version=18)
    task_timer = odoo.add_task_timer(11, minutes_ago=30)
    res = tt.run(odoo, "start", {"project_id": 402, "confirm": True})
    assert [i["timer_id"] for i in res["interrupted"]] == [task_timer]
    assert odoo.timers[task_timer]["timer_pause"]
    # два таймера — стоп без посочване трябва да избере течащия
    prop = tt.run(odoo, "stop", {})
    assert prop["proposal"]["timer"]["res_model"] == tt.LINE


def test_paused_time_is_not_counted():
    odoo = FakeOdoo(version=18)
    tid = odoo.add_task_timer(11, minutes_ago=30)
    odoo.timers[tid]["timer_pause"] = odoo.now.strftime(FMT)
    odoo.advance(120)
    st = tt.run(odoo, "status", {})
    assert st["timers"][0]["elapsed_minutes"] == 30.0
    assert st["timers"][0]["running"] is False


def test_parse_dt_accepts_xmlrpc_formats():
    assert tt._parse_dt("20260929T10:00:00") == datetime(2026, 9, 29, 10, 0)
    assert tt._parse_dt("2026-09-29 10:00:00.123") == datetime(2026, 9, 29, 10, 0)
    assert tt._parse_dt(False) is None
