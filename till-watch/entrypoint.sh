#!/bin/sh
# Записва контейнера в Zero Trust със service token (MDM файл) и пуска WARP,
# после наблюдателя. Тайните са само в env и в mdm.xml (0600, в контейнера).
set -eu
: "${WARP_ORG:?}" "${WARP_CLIENT_ID:?}" "${WARP_CLIENT_SECRET:?}"
umask 077
mkdir -p /var/lib/cloudflare-warp
cat > /var/lib/cloudflare-warp/mdm.xml <<XML
<dict>
  <key>organization</key><string>${WARP_ORG}</string>
  <key>auth_client_id</key><string>${WARP_CLIENT_ID}</string>
  <key>auth_client_secret</key><string>${WARP_CLIENT_SECRET}</string>
  <key>service_mode</key><string>warp</string>
  <key>auto_connect</key><integer>1</integer>
  <key>onboarding</key><false/>
</dict>
XML
warp-svc >/var/log/warp-svc.log 2>&1 &
# warp-svc сам се записва по MDM файла; чакаме „Connected“ до 2 минути
i=0
until warp-cli --accept-tos status 2>/dev/null | grep -q "Connected"; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "till-watch: WARP не се свърза за 2 минути — продължавам, проходите няма да се отчитат" >&2
    warp-cli --accept-tos status >&2 || true
    break
  fi
  sleep 2
done
exec python3 /app/watch.py
