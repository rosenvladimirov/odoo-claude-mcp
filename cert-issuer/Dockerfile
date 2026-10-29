# Сертификатният сървър за касите (ADR erpnet-kasi-fleet/0005).
# ACME-то го прави lego (статичен бинарен файл от официалния образ).
FROM goacme/lego:v4.35.2 AS lego

FROM python:3.12-slim

LABEL maintainer="BL Consulting"
LABEL description="cert-issuer — Let's Encrypt (DNS-01) certificates for the private names of the tills"

COPY --from=lego /lego /usr/local/bin/lego
RUN pip install --no-cache-dir "cryptography>=42,<47" \
    && useradd --system --uid 10095 --home /data issuer \
    && mkdir -p /data && chown issuer /data

WORKDIR /app
COPY issuer.py /app/issuer.py

USER issuer
ENV CERT_ISSUER_DATA=/data PORT=8095
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8095/healthz')"
CMD ["python", "/app/issuer.py", "serve"]
