# Наблюдателят на касите (ADR erpnet-kasi-fleet/0017): WARP клиент + watch.py.
# Имената <каса>.<клиент>.erpnet.mcpworks.net съществуват само в Gateway, затова
# контейнерът е WARP устройство на организацията, записано със service token.
FROM debian:bookworm-slim

LABEL maintainer="BL Consulting"
LABEL description="till-watch — checks the fiscal tills through their tunnels and reports to the license server"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
       | gpg --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" \
       > /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflare-warp \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY watch.py entrypoint.sh /app/
RUN chmod 0755 /app/entrypoint.sh

ENV WATCH_INTERVAL_S=60 WATCH_ALIVE_FILE=/tmp/till-watch.alive PYTHONUNBUFFERED=1
# Жив е, ако последният проход е отпреди по-малко от 3 интервала.
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s CMD python3 -c "import os,time,sys;f=os.environ['WATCH_ALIVE_FILE'];sys.exit(0 if time.time()-os.path.getmtime(f)<3*int(os.environ['WATCH_INTERVAL_S'])+30 else 1)"
ENTRYPOINT ["/app/entrypoint.sh"]
