"""till-enroller по договора v1: цикълът, rollback-ът, зоната на DNS, запечатването, тайните."""

import base64
import json
import logging

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from conftest import CF_TOKEN, CI_ID, CI_SECRET, ENV, LS_KEY, VECTORS, item
from conftest import m

HOST = "kasa1.zzclient.erpnet.mcpworks.net"
LAN = "kasa1.zzclient.lan.mcpworks.net"
BOX_PRIV = m.b64u_dec(VECTORS["box_private_key_b64u"])


def efp1_of(world, ref=item()["till_ref"]):
    return json.loads(m.unseal(BOX_PRIV, world.ls.stored[ref]))


# ─── запечатването (вектори) ─────────────────────────────────────────

def test_vector_sealed_efp1_opens_to_the_plaintext():
    assert m.unseal(BOX_PRIV, VECTORS["sealed_efp1"]).decode() == VECTORS["efp1_plain"]
    assert m.unseal(BOX_PRIV, VECTORS["operator_password_sealed"]).decode() == VECTORS["operator_password_plain"]


def test_my_seal_opens_with_the_vector_box_key():
    plain = VECTORS["efp1_plain"].encode()
    sealed = m.seal(m.b64u_dec(VECTORS["box_pub_b64u"]), plain)
    assert m.unseal(BOX_PRIV, sealed) == plain
    assert m.seal(m.b64u_dec(VECTORS["box_pub_b64u"]), plain) != sealed      # ефимерен ключ всеки път
    raw = m.b64u_dec(sealed)
    assert len(raw) == 32 + len(plain) + 16 and "=" not in sealed


def test_efp1_from_the_vector_fields_is_byte_for_byte_the_vector():
    v = json.loads(VECTORS["efp1_plain"])
    built = m.build_efp1(v["c"], v["k"], v["h"], v["t"], v["tok"], v["reg"], v["pt"], v["cu"], v["ssh"])
    assert m.canonical_json(built) == VECTORS["efp1_plain"]


def test_a_tampered_seal_does_not_open():
    raw = bytearray(m.b64u_dec(VECTORS["sealed_efp1"]))
    raw[-1] ^= 1
    with pytest.raises(Exception):
        m.unseal(BOX_PRIV, m.b64u(bytes(raw)))


# ─── create ──────────────────────────────────────────────────────────

def test_create_full_cycle(world):
    world.ls.queue = [item()]
    assert world.enr.run_once() == {"ok": 1, "failed": 0}
    (t,) = world.cf.tunnels.values()
    assert t["name"] == "erpnet-zzclient-kasa1"
    (r,) = world.cf.routes.values()
    assert r == {"id": r["id"], "hostname": HOST, "tunnel_id": t["id"]}
    assert list(world.ci.tills) == [HOST]
    assert world.ls.reports == [{"till_ref": item()["till_ref"], "cf_tunnel_id": t["id"], "cf_hostname": HOST}]
    efp1 = efp1_of(world)
    assert set(efp1) == {"v", "c", "k", "h", "t", "tok", "reg", "pt", "cu", "crt", "ssh", "cors"}   # без d/fp
    assert efp1["crt"] == "CI-TILL-SECRET-" + HOST         # тайната за certpull от cert-issuer (поправка 1.1)
    assert efp1["v"] == 2 and efp1["c"] == "zzclient" and efp1["k"] == "kasa1" and efp1["h"] == HOST
    assert efp1["t"] == t["id"] and base64.b64decode(efp1["tok"]).decode() == f"TUNNEL-TOKEN-SECRET-{t['id']}"
    assert efp1["reg"] == "https://zzclient.example.com" and efp1["cors"] == ["https://zzclient.example.com"]
    assert efp1["cu"] == "https://certs.mcpworks.net" and efp1["ssh"] == []
    # pt е разрешението на LS; подписът е верен и носи тунела и името от този проход
    grant = json.loads(m.b64u_dec(efp1["pt"]))
    Ed25519PublicKey.from_public_bytes(base64.b64decode(VECTORS["grant_public_key_b64"])).verify(
        base64.b64decode(grant["sig"]), m.canonical_json(grant["payload"]).encode())
    assert grant["payload"]["cf_tunnel_id"] == t["id"] and grant["payload"]["cf_hostname"] == HOST
    (done,) = world.ls.done
    assert done["action"] == "create" and done["ok"] is True and t["id"] in done["note"]
    # LS вече не го дава ⇒ вторият проход не прави нищо
    assert world.enr.run_once() == {"ok": 0, "failed": 0} and len(world.cf.tunnels) == 1


def test_ssh_keys_from_env_go_into_efp1(world):
    enr = m.build(dict(world.env, ENR_SSH_PUBKEYS="ssh-ed25519 AAAA rosen\n\nssh-ed25519 BBBB x"),
                  cf_api=world.cf.url + "/client/v4")
    world.ls.queue = [item()]
    enr.run_once()
    assert efp1_of(world)["ssh"] == ["ssh-ed25519 AAAA rosen", "ssh-ed25519 BBBB x"]


@pytest.mark.parametrize("mock,method,key,step", [
    ("cf", "POST", "route", "route"),
    ("cf", "GET", "token", "token"),
    ("ci", "POST", "tills", "cert-issuer"),
    ("ls", "POST", "report", "report"),
    ("ls", "POST", "store", "store"),
])
def test_failure_after_the_tunnel_rolls_everything_back(world, mock, method, key, step):
    getattr(world, mock).fail[(method, key)] = 500
    world.ls.queue = [item()]
    assert world.enr.run_once() == {"ok": 0, "failed": 1}
    assert world.cf.tunnels == {} and world.cf.routes == {} and world.ci.tills == {}, "сираци"
    assert item()["till_ref"] not in world.ls.stored
    (done,) = world.ls.done
    assert done["ok"] is False and f"failed at {step}" in done["note"] and "rollback clean (checked in CF)" in done["note"]
    # тунел наистина е имало (провалът е след него)
    assert any(r["method"] == "POST" and r["path"].endswith("/cfd_tunnel") for r in world.cf.requests)


def test_a_retry_after_a_failure_succeeds(world):
    world.ls.fail[("POST", "store")] = 500
    world.ls.queue = [item()]
    world.enr.run_once()
    del world.ls.fail[("POST", "store")]
    assert world.enr.run_once() == {"ok": 1, "failed": 0}
    assert len(world.cf.tunnels) == 1 and list(world.ci.tills) == [HOST]


def test_a_tunnel_created_with_a_lost_answer_is_swept_by_the_cf_list(world):
    world.cf.lost_create = True
    world.ls.queue = [item()]
    world.enr.run_once()
    assert world.cf.tunnels == {}
    assert "rollback clean" in world.ls.done[0]["note"]


def test_a_rollback_that_cannot_delete_reports_what_is_left(world):
    world.ls.fail[("POST", "report")] = 500
    world.cf.fail[("DELETE", "tunnel")] = 500
    world.ls.queue = [item()]
    world.enr.run_once()
    (t,) = world.cf.tunnels.values()
    note = world.ls.done[0]["note"]
    assert world.ls.done[0]["ok"] is False and "LEFT" in note and f"tunnel {t['id']}" in note
    assert world.cf.routes == {} and world.ci.tills == {}


def test_an_existing_tunnel_is_not_taken_and_not_deleted(world):
    """Заварена каса (kasi.py, ADR-0014) — не е наша; човек решава."""
    world.cf.tunnels["live"] = {"id": "live", "name": "erpnet-zzclient-kasa1"}
    world.ls.queue = [item()]
    world.enr.run_once()
    assert world.cf.tunnels == {"live": {"id": "live", "name": "erpnet-zzclient-kasa1"}}
    assert world.ls.done[0]["ok"] is False and "already exists" in world.ls.done[0]["note"]
    assert not [r for r in world.cf.requests if r["method"] != "GET"] and world.ci.tills == {}


def test_an_existing_route_is_not_taken(world):
    world.cf.routes["r"] = {"id": "r", "hostname": HOST, "tunnel_id": "other"}
    world.ls.queue = [item()]
    world.enr.run_once()
    assert world.cf.routes == {"r": {"id": "r", "hostname": HOST, "tunnel_id": "other"}} and world.cf.tunnels == {}


@pytest.mark.parametrize("bad", [{"client": "ZZ"}, {"till": "kasa_1"}, {"till": "a" * 31}, {"box_pub": "xx"},
                                 {"client": "zz.evil"}, {"action": "explode"}])
def test_bad_records_touch_nothing(world, bad):
    world.ls.queue = [item(**bad)]
    assert world.enr.run_once() == {"ok": 0, "failed": 1}
    assert not [r for r in world.cf.requests if r["method"] != "GET"] and world.ci.tills == {}
    assert world.ls.done[0]["ok"] is False


def test_one_failing_till_does_not_stop_the_others(world):
    world.ls.queue = [item(ref="r1", till="kasa1", box_pub="bad"), item(ref="r2", till="kasa2")]
    assert world.enr.run_once() == {"ok": 1, "failed": 1}
    assert list(world.ci.tills) == ["kasa2.zzclient.erpnet.mcpworks.net"]


# ─── lan ─────────────────────────────────────────────────────────────

def test_lan_creates_a_dns_only_record_and_the_san(world):
    world.ls.queue = [item()]
    world.enr.run_once()
    world.ls.queue = [item("lan", lan_ip="192.168.0.140")]
    assert world.enr.run_once() == {"ok": 1, "failed": 0}
    (rec,) = world.cf.dns.values()
    assert (rec["type"], rec["name"], rec["content"], rec["proxied"]) == ("A", LAN, "192.168.0.140", False)
    assert world.ci.tills[HOST]["extra"] == [LAN]
    assert world.ls.done[-1] == {"till_ref": item()["till_ref"], "action": "lan", "ok": True,
                                 "note": f"A {LAN} -> 192.168.0.140"}
    # смяна на адреса (DHCP) — същият запис се обновява
    world.ls.queue = [item("lan", lan_ip="192.168.0.135")]
    world.enr.run_once()
    (rec,) = world.cf.dns.values()
    assert rec["content"] == "192.168.0.135" and "(was 192.168.0.140)" in world.ls.done[-1]["note"]


@pytest.mark.parametrize("ip", ["8.8.8.8", "100.64.0.1", "127.0.0.1", "169.254.1.1", "fd00::1", "192.168.0", ""])
def test_lan_ip_must_be_rfc1918(world, ip):
    world.ls.queue = [item("lan", lan_ip=ip)]
    world.enr.run_once()
    assert world.cf.dns_writes() == [] and world.ls.done[0]["ok"] is False


def test_lan_rollback_when_the_san_fails(world):
    world.ls.queue = [item()]
    world.enr.run_once()
    world.ci.fail[("POST", "san")] = 500
    world.ls.queue = [item("lan", lan_ip="10.1.2.3")]
    world.enr.run_once()
    assert world.cf.dns == {} and world.ls.done[-1]["ok"] is False


# ─── DNS само под *.lan.mcpworks.net ─────────────────────────────────

@pytest.mark.parametrize("name", [
    "kasa1.zzclient.mcpworks.net", "www.mcpworks.net", "mcpworks.net", "lan.mcpworks.net",
    "zzclient.lan.mcpworks.net", "x.kasa1.zzclient.lan.mcpworks.net", "kasa1.zzclient.lan.mcpworks.net.evil.com",
    "kasa1.zzclient.evil-lan.mcpworks.net", "kasa1.zzclient.erpnet.mcpworks.net", "KASA1.zzclient.lan.mcpworks.net",
    "*.zzclient.lan.mcpworks.net", "kasa1..lan.mcpworks.net", "",
])
def test_dns_outside_the_lan_zone_is_refused_before_any_call(world, name):
    cf = world.enr.cf
    for call in (lambda: cf.dns_upsert_a(name, "10.0.0.1", "x"), lambda: cf.dns_delete(name),
                 lambda: cf.dns_find(name)):
        with pytest.raises(m.LanZoneError):
            call()
    assert [r for r in world.cf.requests if "/dns_records" in r["path"]] == []


def test_the_dry_run_cf_refuses_too():
    with pytest.raises(m.LanZoneError):
        m.DryRunCloudflare().dns_upsert_a("www.mcpworks.net", "10.0.0.1", "x")


def test_the_lan_zone_is_a_constant_not_env(monkeypatch):
    monkeypatch.setenv("LAN_SUFFIX", "mcpworks.net")
    with pytest.raises(m.LanZoneError):
        m.check_lan_name("kasa1.zzclient.mcpworks.net")


# ─── retire ──────────────────────────────────────────────────────────

def test_retire_removes_everything(world):
    world.ls.queue = [item()]
    world.enr.run_once()
    world.ls.queue = [item("lan", lan_ip="172.16.5.5")]
    world.enr.run_once()
    world.cf.tunnels["other"] = {"id": "other", "name": "erpnet-zzclient-kasa2"}    # чуждата каса остава
    world.ls.queue = [item("retire")]
    assert world.enr.run_once() == {"ok": 1, "failed": 0}
    assert world.cf.tunnels == {"other": {"id": "other", "name": "erpnet-zzclient-kasa2"}}
    assert world.cf.routes == {} and world.cf.dns == {} and world.ci.tills == {}
    assert world.ls.done[-1]["action"] == "retire" and world.ls.done[-1]["ok"] is True


def test_retire_of_a_till_with_nothing_left_is_ok(world):
    world.ls.queue = [item("retire")]
    assert world.enr.run_once() == {"ok": 1, "failed": 0}


def test_retire_that_cannot_delete_is_not_ok(world):
    world.ls.queue = [item()]
    world.enr.run_once()
    world.cf.fail[("DELETE", "tunnel")] = 500
    world.ls.queue = [item("retire")]
    world.enr.run_once()
    assert world.ls.done[-1]["ok"] is False and "LEFT" in world.ls.done[-1]["note"]
    assert world.ci.tills == {}      # другото е махнато


# ─── сух режим, удостоверяване, тайни ─────────────────────────────────

def test_dry_run_makes_no_cloudflare_calls(world):
    env = {k: v for k, v in world.env.items() if not k.startswith("CF_")}
    enr = m.build(dict(env, ENR_DRY_RUN="1"), cf_api=world.cf.url + "/client/v4")
    assert isinstance(enr.cf, m.DryRunCloudflare)
    world.ls.queue = [item()]
    assert enr.run_once() == {"ok": 1, "failed": 0}
    world.ls.queue = [item("lan", lan_ip="10.0.0.9")]
    assert enr.run_once() == {"ok": 1, "failed": 0}
    world.ls.queue = [item("retire")]
    assert enr.run_once() == {"ok": 1, "failed": 0}
    assert world.cf.requests == []
    assert world.ls.reports[0]["cf_tunnel_id"].startswith("dry-")


def test_missing_env_is_refused():
    with pytest.raises(SystemExit):
        m.build({"LS_URL": "x"})
    with pytest.raises(SystemExit):     # без CF — само в сух режим
        m.build({k: v for k, v in dict(ENV, LS_URL="x", CI_URL="y").items() if k != "CF_ZONE_ID"})


def test_the_ls_key_goes_as_bearer_and_a_wrong_one_fails_the_pass(world):
    assert all(r["headers"]["Authorization"] == "Bearer " + LS_KEY for r in world.ls.requests)
    enr = m.build(dict(world.env, LS_API_KEY="wrong-key-123"), cf_api=world.cf.url + "/client/v4")
    world.ls.queue = [item()]
    with pytest.raises(m.HttpError) as e:
        enr.run_once()
    assert e.value.status == 401 and "wrong-key-123" not in str(e.value)


def test_secrets_never_reach_the_log_or_the_notes(world, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    world.ls.queue = [item(ref="ok-1"), item(ref="bad-1", till="kasa2", box_pub="bad")]
    world.enr.run_once()
    world.ls.fail[("POST", "store")] = 500
    world.ls.queue = [item(ref="bad-2", till="kasa3")]
    world.enr.run_once()
    world.ls.queue = [item("lan", ref="ok-1", lan_ip="10.9.9.9"), item("retire", ref="ok-1")]
    world.enr.run_once()
    tok_b64 = efp1_of(world, "ok-1")["tok"]
    crt = efp1_of(world, "ok-1")["crt"]
    assert crt == "CI-TILL-SECRET-" + HOST        # crt е в разпечатания EFP1…
    text = caplog.text + capsys.readouterr().err + json.dumps(world.ls.done)
    assert len(caplog.records) >= 5, "логът е празен — тестът не мери нищо"
    for secret in (CF_TOKEN, LS_KEY, CI_SECRET, CI_ID, tok_b64, base64.b64decode(tok_b64).decode(),
                   crt, "CI-TILL-SECRET", world.ls.stored["ok-1"]):      # …а в лога и бележките — не
        assert secret not in text


def test_the_redactor_catches_a_secret_logged_by_mistake(caplog):
    caplog.set_level(logging.INFO)
    m.REDACT.add("leaky-token-ABCDEF")
    m.log.info("oops %s", "leaky-token-ABCDEF")
    try:
        raise RuntimeError("leaky-token-ABCDEF")
    except RuntimeError:
        m.log.exception("boom")
    assert "leaky-token-ABCDEF" not in caplog.text and "oops ***" in caplog.text


def test_crt_is_kept_nowhere_but_in_the_sealed_efp1(world):
    world.ls.queue = [item()]
    world.enr.run_once()
    crt = efp1_of(world)["crt"]
    sent_to_ls = json.dumps([r["body"] for r in world.ls.requests])
    assert crt not in sent_to_ls and crt not in json.dumps(vars(world.enr), default=str)


def test_a_crt_logged_by_mistake_is_scrubbed(world, caplog):
    caplog.set_level(logging.INFO)
    world.ls.queue = [item()]
    world.enr.run_once()
    crt = efp1_of(world)["crt"]
    m.log.info("debug %s", crt)
    assert crt not in caplog.text and "debug ***" in caplog.text


def test_cert_issuer_without_a_secret_rolls_back(world, monkeypatch):
    monkeypatch.setattr(m.CertIssuer, "_call", lambda self, method, path, body=None: {})
    world.ls.queue = [item()]
    world.enr.run_once()
    assert world.cf.tunnels == {} and world.ls.done[0]["ok"] is False and "failed at cert-issuer" in world.ls.done[0]["note"]
