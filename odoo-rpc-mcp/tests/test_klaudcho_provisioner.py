"""Тестове на изпълнителя на Клаудчо (klaudcho_provisioner.py, ADR telegram-consult-bot/0009).

Без истински Odoo, Telegram и Portainer: фалшивите пазят какво им е поискано.

    pytest tests/test_klaudcho_provisioner.py -v
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import klaudcho_provisioner as kp  # noqa: E402

ENV = {"IMAGE": "klaudcho-agent:0.8.3", "NETWORK": "terraros-v19_terraros-v19-net",
       "KNOWLEDGE_DIR": "/opt/claude-knowledge/out", "AGENT_ODOO_API_KEY": "bot-key", "ANTHROPIC_API_KEY": "sk-ant-x",
       "START_TIMEOUT": "20"}


def tg_job(sid=7):
    return {"service_id": sid, "step": "telegram", "order": "S00002", "title": "S00002 — Клиент ООД",
            "members": ["@ivan"], "bot_username": "kl_bot"}


def container_job(sid=8):
    return {"service_id": sid, "step": "container", "slug": "klaudcho-s00002", "agent_toml": 'agent_id = "x"\n',
            "control_token": "ctl-abc", "read_token": "ro-abc", "odoo_agent_id": 3}


class FakeOdoo:
    def __init__(self, jobs, lose_reports=0):
        self.jobs = list(jobs)
        self.reports = []
        self.lose_reports = lose_reports

    def execute_kw(self, model, method, args, kwargs):
        assert model == "klaudcho.service"
        if method == "klaudcho_provision_pending":
            self.asked_steps = kwargs.get("steps")
            return [j for j in self.jobs if j["step"] in kwargs.get("steps", [j["step"]])]
        assert method == "klaudcho_provision_report"
        if self.lose_reports:
            self.lose_reports -= 1
            raise ConnectionError("lost after commit")
        (ids, step, ok) = args
        self.reports.append((ids[0], step, ok, kwargs["data"], kwargs["error"]))
        if ok:
            self.jobs = [j for j in self.jobs if j["service_id"] != ids[0]]
        return "ready" if ok else step


class FakeTg:
    def __init__(self, bot_added=True):
        self.calls = []
        self.bot_added = bot_added

    def create_group(self, title, members, bot_username=""):
        self.calls.append((title, members, bot_username))
        return {"status": "created", "chat_id": "-1001234", "invite_link": "https://t.me/+abc",
                "bot_added": self.bot_added, "added": members, "failed": []}


class FakePortainer:
    def __init__(self, running_after=1):
        self.created, self.deleted = [], []
        self.checks = 0
        self.running_after = running_after

    def create_stack(self, name, compose):
        self.created.append((name, compose))
        return {"ok": True}

    def delete_stack(self, name):
        self.deleted.append(name)
        return {"ok": True}

    def container_running(self, name):
        self.checks += 1
        return self.checks >= self.running_after


def make(tmp_path, jobs, live=True, tg=None, portainer=None, **kw):
    odoo = FakeOdoo(jobs, **kw)
    tg = tg or FakeTg()
    portainer = portainer or FakePortainer()
    p = kp.Provisioner(odoo, tg=lambda: tg, portainer=portainer, journal=kp.Journal(str(tmp_path / "j.jsonl")),
                       env=dict(ENV), live=live, clock=lambda s: None)
    return p, odoo, tg, portainer


def test_dry_run_creates_nothing_but_reports(tmp_path):
    p, odoo, tg, port = make(tmp_path, [tg_job(), container_job()], live=False)
    p.run_once()
    assert tg.calls == [] and port.created == []
    assert [(r[1], r[2], r[3].get("dry_run")) for r in odoo.reports] == [("telegram", True, True), ("container", True, True)]


def test_the_group_comes_from_the_publisher_with_the_bot(tmp_path):
    p, odoo, tg, _ = make(tmp_path, [tg_job()])
    p.run_once()
    assert tg.calls == [("S00002 — Клиент ООД", ["@ivan"], "kl_bot")]
    assert odoo.reports[0][:3] == (7, "telegram", True)
    assert odoo.reports[0][3]["chat_id"] == "-1001234" and odoo.reports[0][3]["invite_link"] == "https://t.me/+abc"


def test_a_group_without_the_bot_is_an_error(tmp_path):
    p, odoo, _, _ = make(tmp_path, [tg_job()], tg=FakeTg(bot_added=False))
    p.run_once()
    sid, step, ok, _data, error = odoo.reports[0]
    assert (sid, step, ok) == (7, "telegram", False) and "bot was not added" in error


def test_a_lost_report_does_not_make_a_second_group(tmp_path):
    p, odoo, tg, _ = make(tmp_path, [tg_job()], lose_reports=2)   # отчетът и докладът за грешка се губят
    p.run_once()
    assert len(tg.calls) == 1 and odoo.reports == []
    p.run_once()                                                     # Odoo пак дава задачата
    assert len(tg.calls) == 1                                        # групата не се прави втори път
    assert odoo.reports[0][:3] == (7, "telegram", True) and odoo.reports[0][3]["chat_id"] == "-1001234"


def test_the_container_stack_is_rendered_and_waited_for(tmp_path):
    p, odoo, _, port = make(tmp_path, [container_job()], portainer=FakePortainer(running_after=3))
    p.run_once()
    ((name, compose),) = port.created
    assert name == "klaudcho-s00002"
    assert "container_name: klaudcho-s00002" in compose and "image: klaudcho-agent:0.8.3" in compose
    assert base64.b64encode(b'agent_id = "x"\n').decode() in compose
    assert "${" not in compose and port.checks == 3
    assert odoo.reports[0][:3] == (8, "container", True)


def test_a_container_that_does_not_start_is_reported(tmp_path):
    p, odoo, _, _ = make(tmp_path, [container_job()], portainer=FakePortainer(running_after=999))
    p.run_once()
    assert odoo.reports[0][2] is False and "TimeoutError" in odoo.reports[0][4]


def test_missing_settings_stop_before_portainer(tmp_path):
    p, odoo, _, port = make(tmp_path, [container_job()])
    p.env["IMAGE"] = ""
    p.run_once()
    assert port.created == [] and "IMAGE" in odoo.reports[0][4]


def test_unsafe_values_do_not_reach_the_yaml():
    job = container_job()
    job["control_token"] = 'x"\n  privileged: true'
    with pytest.raises(ValueError, match="unsafe"):
        kp.render_stack(job, ENV)


def test_teardown_deletes_the_stack(tmp_path):
    job = dict(container_job(), step="teardown")
    p, odoo, _, port = make(tmp_path, [job])
    p.run_once()
    assert port.deleted == ["klaudcho-s00002"] and odoo.reports[0][:3] == (8, "teardown", True)


def test_wake_needs_the_secret(monkeypatch, tmp_path):
    p, *_ = make(tmp_path, [])
    monkeypatch.setattr(kp, "_instance", p)
    monkeypatch.setenv("KLAUDCHO_PROV_WAKE_TOKEN", "s3cret")
    kp._wake.clear()

    def call(token):
        sent = []

        async def receive():
            return {"body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        scope = {"method": "POST", "headers": [(b"x-klaudcho-wake", token.encode())]}
        asyncio.run(kp.wake_asgi(scope, receive, send))
        return sent[0]["status"]

    assert call("wrong") == 401 and not kp._wake.is_set()
    assert call("грешна") == 401 and not kp._wake.is_set()        # не-ASCII заглавка: 401, не 500
    assert call("s3cret") == 200 and kp._wake.is_set()


def test_without_an_api_key_the_agent_runs_on_the_fake_model(tmp_path):
    p, odoo, _, port = make(tmp_path, [container_job()])
    p.env["ANTHROPIC_API_KEY"] = ""
    p.run_once()
    assert port.created and odoo.reports[0][2] is True


def test_a_provisioner_asks_only_for_its_steps(tmp_path):
    p, odoo, tg, port = make(tmp_path, [tg_job(), container_job()])
    p.steps = ("container", "teardown")
    p.run_once()
    assert odoo.asked_steps == ["container", "teardown"]
    assert tg.calls == [] and len(port.created) == 1


def test_only_klaudcho_stacks_are_touched():
    job = dict(container_job(), slug="terraros-v19")
    with pytest.raises(ValueError, match="only klaudcho"):
        kp.render_stack(job, ENV)
    rt = kp.DockerRuntime(run=lambda *a, **k: pytest.fail("docker must not be called"))
    for call in (lambda: rt.delete_stack("terraros-v19-odoo"), lambda: rt.container_running("x; rm -rf /"),
                 lambda: rt.create_stack("../klaudcho-x", "")):
        with pytest.raises(ValueError):
            call()


def test_docker_runtime_commands():
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        return R(out="true false\n") if cmd[1] == "inspect" else R()

    rt = kp.DockerRuntime(run=run)
    assert rt.create_stack("klaudcho-s00002", "services: {}") == {"ok": True}
    assert rt.container_running("klaudcho-s00002") is True
    assert rt.delete_stack("klaudcho-s00002") == {"ok": True}
    assert calls[0] == (["docker", "compose", "-p", "klaudcho-s00002", "-f", "-", "up", "-d", "--remove-orphans"],
                        "services: {}")
    assert calls[2][0] == ["docker", "compose", "-p", "klaudcho-s00002", "down"]      # без -v: томът остава


def test_dollar_is_unsafe_in_values():
    job = container_job()
    job["read_token"] = "abc${HOME}"
    with pytest.raises(ValueError, match="unsafe"):
        kp.render_stack(job, ENV)


def test_the_agent_gets_the_bot_key_not_the_provisioner_key(monkeypatch):
    monkeypatch.setenv("KLAUDCHO_PROV_ODOO_URL", "https://odoo.example")
    monkeypatch.setenv("KLAUDCHO_PROV_ODOO_API_KEY", "provisioner-key")
    monkeypatch.setenv("KLAUDCHO_PROV_AGENT_ODOO_API_KEY", "bot-key")
    for k in ("IMAGE", "NETWORK", "KNOWLEDGE_DIR"):
        monkeypatch.setenv(f"KLAUDCHO_PROV_{k}", ENV[k])

    class Conn:
        def __init__(self, **kw):
            self.kw = kw

    p = kp.build_from_env(Conn, lambda: None, engine=None)
    assert p.odoo.kw["api_key"] == "provisioner-key"
    compose = kp.render_stack(container_job(), p.env)
    assert "KLAUDCHO_ODOO_API_KEY: bot-key" in compose and "provisioner-key" not in compose
