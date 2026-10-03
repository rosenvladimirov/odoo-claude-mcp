#!/bin/bash
# Odoo инстанция → драйвер syslog (127.0.0.1:601 → Alloy → Loki). Употреба: odoo_to_syslog.sh <compose_dir> <service> <container>
# Само за стекове, при които мрежите в compose съвпадат с живите (проверено преди това, 03.10.2026).
set -euo pipefail
D=$1; SVC=$2; C=$3
cd "$D"
F=$(docker inspect "$C" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
nets_before=$(docker inspect "$C" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{join $v.Aliases ","}} {{end}}' | tr ' ' '\n' | sort | tr '\n' ' ')
cp -p "$F" "$F.bak-pre-syslog-$(date -u +%Y%m%d-%H%M)"
python3 - "$F" "$C" <<'PY'
import sys
f, c = sys.argv[1], sys.argv[2]
s = open(f).read()
a = f"    container_name: {c}\n"
assert s.count(a) == 1, f"container_name {c}: {s.count(a)} съвпадения"
i = s.index(a) + len(a)
nxt = s.find("\n  ", i)              # следващата услуга на ниво 2
block = s[i: nxt if nxt != -1 else len(s)]
assert "logging:" not in block, "вече има logging"
s = s[:i] + f"""    logging:  # към Loki през Alloy (127.0.0.1:601), 03.10.2026
      driver: syslog
      options:
        syslog-address: "tcp://127.0.0.1:601"
        syslog-format: rfc5424
        tag: "{{{{.Name}}}}"
        mode: non-blocking
        max-buffer-size: 4m
""" + s[i:]
open(f, "w").write(s)
PY
drv=$(docker compose config --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['services']['$SVC'].get('logging',{}).get('driver'))")
test "$drv" = "syslog"
docker compose up -d --no-deps "$SVC" >/dev/null 2>&1
sleep 12
nets_after=$(docker inspect "$C" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{join $v.Aliases ","}} {{end}}' | tr ' ' '\n' | sort | tr '\n' ' ')
st=$(docker inspect "$C" --format '{{.State.Status}} {{.HostConfig.LogConfig.Type}}')
same=$([ "$(echo $nets_before | tr ',' '\n' | sort -u)" = "$(echo $nets_after | tr ',' '\n' | sort -u)" ] && echo "мрежите: същите" || echo "МРЕЖИТЕ СЕ ПРОМЕНИХА: $nets_before → $nets_after")
echo "$C: $st | $same"
