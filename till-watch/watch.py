"""till-watch — наблюдателят на касите (ADR erpnet-kasi-fleet/0017).

Цикъл на WATCH_INTERVAL_S (60) секунди:
    1. LS `POST /l10n_bg/erpnet/watch/targets` → касите с тунел;
    2. `GET http://<cf_hostname>:<WATCH_PORT>/healthz` на всяка, паралелно,
       през WARP (имената съществуват само в Gateway — ADR-0002);
    3. LS `POST /l10n_bg/erpnet/watch/report` → {ok, version, devices, error}.

Ако самият WARP не е свързан, проходът НЕ се отчита: иначе всички каси
стават „няма връзка“ заради нас. Лицензионният сървър сам ги връща в
„неизвестно“, когато наблюдателят мълчи над 15 минути.

Env (тайните — само оттук; нищо от тях не влиза в лога):
    LS_URL, LS_API_KEY             лицензионният сървър, потребител от group_till_watcher
    WATCH_INTERVAL_S (60)          между проходите
    WATCH_TIMEOUT_S (8)            за една каса
    WATCH_PORT (8001)              портът на проксито зад тунела
    WATCH_WORKERS (16)             паралелни проверки
    WATCH_WARP_CHECK (1)           0 — без проверка на WARP (тестове, ръчно пускане)
    WATCH_ALIVE_FILE (/tmp/till-watch.alive)   за HEALTHCHECK-а
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

log = logging.getLogger("till-watch")

# Имената, които наблюдателят изобщо пита — нищо извън нашата CF мрежа
ERPNET_SUFFIX = ".erpnet.mcpworks.net"
# Таван на един отчет (същият като на LS)
REPORT_CHUNK = 500
# Таван на отговора на /healthz — касата не може да ни напълни паметта
HEALTHZ_MAX = 256 * 1024
# Cloudflare пред лицензионния сървър спира подразбиращия се клиент на Python
# (грешка 1010, измерено 14.11) — наблюдателят се представя със свое име
USER_AGENT = "till-watch/1.0 (+ADR erpnet-kasi-fleet/0017)"


# ─── една каса ─────────────────────────────────────────────────────────

def _no_redirect_opener() -> urllib.request.OpenerDirector:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):  # noqa: D401 — пренасочване = грешка
            return None
    # без прокси от средата: пътят е само през WARP
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


_OPENER = _no_redirect_opener()


def check_till(host: str, port: int, timeout: float) -> dict:
    """`/healthz` на една каса → `{ok, version?, devices?, error?}`. Никога не хвърля.

    Първо `https://<име>/healthz` (каса с Traefik на 443 — Баръмски, Теолино),
    после `http://<име>:<port>/healthz` (пакетите без Traefik). Грешката е и от двата.
    """
    if not isinstance(host, str) or not host.endswith(ERPNET_SUFFIX) or "/" in host or ":" in host:
        return {"ok": False, "error": "bad_hostname"}
    first = _check_url("https://%s/healthz" % host, timeout)
    if first["ok"]:
        return first
    second = _check_url("http://%s:%d/healthz" % (host, port), timeout)
    if second["ok"]:
        return second
    return {"ok": False, "error": ("https: %s; http: %s" % (first["error"], second["error"]))[:200]}


def _check_url(url: str, timeout: float) -> dict:
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(HEALTHZ_MAX + 1)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "http_%d" % exc.code}
    except urllib.error.URLError as exc:
        reason = exc.reason
        return {"ok": False, "error": type(reason).__name__ if not isinstance(reason, str) else reason[:80]}
    except (TimeoutError, OSError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    if len(body) > HEALTHZ_MAX:
        return {"ok": False, "error": "too_large"}
    try:
        data = json.loads(body)
    except ValueError:
        return {"ok": False, "error": "bad_json"}
    if not isinstance(data, dict) or data.get("ok") is not True:
        return {"ok": False, "error": "not_ok"}
    out = {"ok": True}
    if isinstance(data.get("version"), str):
        out["version"] = data["version"][:64]
    # броят на устройствата по вид — само списъците в отговора
    out["devices"] = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    return out


# ─── лицензионният сървър ──────────────────────────────────────────────

class LS:
    def __init__(self, url: str, api_key: str, timeout: float = 30):
        self.url = url.rstrip("/")
        self._key = api_key
        self.timeout = timeout

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key,
                     "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def targets(self) -> list[dict]:
        rows = self.post("/l10n_bg/erpnet/watch/targets", {}).get("tills")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def report(self, checks: list[dict]) -> int:
        updated = 0
        for i in range(0, len(checks), REPORT_CHUNK):
            updated += int(self.post("/l10n_bg/erpnet/watch/report",
                                     {"checks": checks[i:i + REPORT_CHUNK]}).get("updated") or 0)
        return updated


# ─── WARP ──────────────────────────────────────────────────────────────

def warp_connected() -> bool:
    try:
        out = subprocess.run(["warp-cli", "--accept-tos", "status"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "Connected" in out and "Disconnected" not in out


# ─── проходът ──────────────────────────────────────────────────────────

def run_once(ls: LS, *, port: int, timeout: float, workers: int,
             warp_ok: Callable[[], bool], check: Callable[..., dict] = check_till) -> Optional[int]:
    """Един проход. Връща броя обновени каси или None, ако не е отчетено."""
    targets = ls.targets()
    if not targets:
        return 0
    if not warp_ok():
        log.warning("WARP не е свързан — проходът не се отчита (%d каси)", len(targets))
        return None
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda t: check(t.get("cf_hostname"), port, timeout), targets))
    checks = [{"till_ref": t.get("till_ref"), **r} for t, r in zip(targets, results)]
    # и WARP може да падне по време на прохода — тогава всичко е „грешка“ заради нас
    if not any(c["ok"] for c in checks) and not warp_ok():
        log.warning("WARP падна по време на прохода — не се отчита")
        return None
    updated = ls.report(checks)
    down = [c["till_ref"] for c in checks if not c["ok"]]
    log.info("Проход: %d каси, %d на линия, %d без връзка %s",
             len(checks), len(checks) - len(down), len(down), down[:10] if down else "")
    return updated


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = os.environ
    ls = LS(env["LS_URL"], env["LS_API_KEY"])
    every = int(env.get("WATCH_INTERVAL_S", "60"))
    port = int(env.get("WATCH_PORT", "8001"))
    timeout = float(env.get("WATCH_TIMEOUT_S", "8"))
    workers = int(env.get("WATCH_WORKERS", "16"))
    warp_ok = warp_connected if env.get("WATCH_WARP_CHECK", "1") != "0" else (lambda: True)
    alive = env.get("WATCH_ALIVE_FILE", "/tmp/till-watch.alive")
    log.info("till-watch: LS %s, на всеки %d s", ls.url, every)
    while True:
        try:
            run_once(ls, port=port, timeout=timeout, workers=workers, warp_ok=warp_ok)
            with open(alive, "w") as fh:
                fh.write(str(int(time.time())))
        except urllib.error.HTTPError as exc:
            log.error("LS отказа: HTTP %d", exc.code)
        except Exception as exc:  # noqa: BLE001 — цикълът не спира
            log.error("Проходът падна: %s", type(exc).__name__)
        time.sleep(every)


if __name__ == "__main__":
    sys.exit(main())
