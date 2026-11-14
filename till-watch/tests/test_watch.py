"""till-watch: проверката на една каса и проходът (ADR erpnet-kasi-fleet/0017)."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import watch  # noqa: E402

HOST = "kasa.zzclient.erpnet.mcpworks.net"


class FakeTill(BaseHTTPRequestHandler):
    status = 200
    body = {"ok": True, "version": "3.1.4", "printers": [{}], "scales": [], "features": {}}

    def do_GET(self):  # noqa: N802
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture
def till():
    srv = HTTPServer(("127.0.0.1", 0), FakeTill)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    # името под erpnet.mcpworks.net сочи към фалшивата каса
    import socket
    orig = socket.getaddrinfo

    def fake(host, *a, **kw):
        return orig("127.0.0.1" if host == HOST else host, *a, **kw)
    with mock.patch("socket.getaddrinfo", fake):
        yield port
    srv.shutdown()
    FakeTill.status, FakeTill.body = 200, {"ok": True, "version": "3.1.4", "printers": [{}], "scales": [], "features": {}}


def test_ok_gives_version_and_device_counts(till):
    r = watch.check_till(HOST, till, 3)
    assert r == {"ok": True, "version": "3.1.4", "devices": {"printers": 1, "scales": 0}}


def test_http_error_and_bad_body(till):
    FakeTill.status = 401
    assert watch.check_till(HOST, till, 3) == {"ok": False, "error": "http_401"}
    FakeTill.status, FakeTill.body = 200, b"<html>"
    assert watch.check_till(HOST, till, 3) == {"ok": False, "error": "bad_json"}
    FakeTill.body = {"ok": False}
    assert watch.check_till(HOST, till, 3) == {"ok": False, "error": "not_ok"}


def test_refused_is_an_error_not_an_exception():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    orig = socket.getaddrinfo
    with mock.patch("socket.getaddrinfo", lambda h, *a, **k: orig("127.0.0.1", *a, **k)):
        r = watch.check_till(HOST, port, 2)
    assert r["ok"] is False and r["error"]


@pytest.mark.parametrize("host", ["evil.example.com", "x.erpnet.mcpworks.net/../a", "x.erpnet.mcpworks.net:1", None])
def test_only_our_names(host):
    assert watch.check_till(host, 8001, 1) == {"ok": False, "error": "bad_hostname"}


class FakeLS:
    def __init__(self, targets):
        self._targets = targets
        self.reports = []

    def targets(self):
        return self._targets

    def report(self, checks):
        self.reports.append(checks)
        return len(checks)


TARGETS = [{"till_ref": "a", "cf_hostname": "a" + watch.ERPNET_SUFFIX},
           {"till_ref": "b", "cf_hostname": "b" + watch.ERPNET_SUFFIX}]


def test_round_reports_each_till():
    ls = FakeLS(TARGETS)
    res = {"a" + watch.ERPNET_SUFFIX: {"ok": True, "version": "3.1.4"},
           "b" + watch.ERPNET_SUFFIX: {"ok": False, "error": "timeout"}}
    n = watch.run_once(ls, port=8001, timeout=1, workers=2, warp_ok=lambda: True,
                       check=lambda h, p, t: res[h])
    assert n == 2
    assert ls.reports == [[{"till_ref": "a", "ok": True, "version": "3.1.4"},
                           {"till_ref": "b", "ok": False, "error": "timeout"}]]


def test_no_warp_no_report():
    ls = FakeLS(TARGETS)
    n = watch.run_once(ls, port=8001, timeout=1, workers=2, warp_ok=lambda: False,
                       check=lambda h, p, t: {"ok": False, "error": "x"})
    assert n is None and ls.reports == []


def test_warp_drops_mid_round_all_failed_no_report():
    ls = FakeLS(TARGETS)
    state = iter([True, False])
    n = watch.run_once(ls, port=8001, timeout=1, workers=2, warp_ok=lambda: next(state),
                       check=lambda h, p, t: {"ok": False, "error": "x"})
    assert n is None and ls.reports == []


def test_all_down_with_warp_up_is_reported():
    ls = FakeLS(TARGETS)
    n = watch.run_once(ls, port=8001, timeout=1, workers=2, warp_ok=lambda: True,
                       check=lambda h, p, t: {"ok": False, "error": "x"})
    assert n == 2 and len(ls.reports[0]) == 2


def test_report_is_chunked():
    sent = []

    class L(watch.LS):
        def post(self, path, body):
            sent.append(len(body["checks"]))
            return {"updated": len(body["checks"])}
    assert L("https://ls", "k").report([{"till_ref": str(i), "ok": True} for i in range(1201)]) == 1201
    assert sent == [500, 500, 201]
