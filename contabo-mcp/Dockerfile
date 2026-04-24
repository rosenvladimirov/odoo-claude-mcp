FROM python:3.12-slim

LABEL maintainer="BL Consulting"
LABEL description="Contabo MCP server — Customer API + S3 Object Storage (stdio→SSE via supergateway)"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g supergateway \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

ENV MCP_PORT=8093
EXPOSE ${MCP_PORT}

CMD ["sh", "-c", "supergateway --stdio 'python /app/server.py' --port ${MCP_PORT}"]
