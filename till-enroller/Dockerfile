# Услугата за записване на касите (ADR erpnet-kasi-fleet/0012). Без порт: само изходящи
# повиквания към лицензионния сървър, Cloudflare API и cert-issuer (през Access).
FROM python:3.12-slim

LABEL maintainer="BL Consulting"
LABEL description="till-enroller — tunnels, routes and sealed EFP1 for the tills accepted on the license server"

RUN pip install --no-cache-dir "cryptography>=42,<47" \
    && useradd --system --uid 10096 --no-create-home enroller

WORKDIR /app
COPY enroller.py /app/enroller.py

USER enroller
ENV ENR_INTERVAL_S=30 ENR_ALIVE_FILE=/tmp/till-enroller.alive PYTHONUNBUFFERED=1
# Жив е, ако последният проход е отпреди по-малко от 3 интервала.
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import os,time,sys;f=os.environ['ENR_ALIVE_FILE'];sys.exit(0 if time.time()-os.path.getmtime(f)<3*int(os.environ['ENR_INTERVAL_S'])+30 else 1)"
CMD ["python", "/app/enroller.py"]
