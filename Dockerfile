FROM python:3.12-slim

# mcp 2.0 removed mcp.server.fastmcp, which this server is built on - keep the pin.
RUN pip install --no-cache-dir "mcp>=1.10,<2" "httpx>=0.27" "python-dotenv>=1.0"

WORKDIR /app
COPY wp_ops_mcp /app/wp_ops_mcp
COPY scripts /app/scripts

# Credentials are MOUNTED, never baked in:
#   docker run --rm -i -v /abs/path/to/creds:/creds:ro wp-ops-mcp
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    WPOPS_TRANSPORT=rest \
    WPOPS_CREDENTIALS=/creds/credentials.json

# Profile cache lives here; mount a volume to persist discovery between runs (optional).
RUN mkdir -p /app/data/wp_ops_mcp

ENTRYPOINT ["python", "-m", "wp_ops_mcp.server"]
