# FinHub MCP: one image for both the HTTP MCP server and the alert daemon (see docker-compose.yml).
#
# Vietnam data needs vnstock 4.x, which can no longer be installed from PyPI. Run
#   .venv/bin/python scripts/freeze_vnstock.py
# once before building; it stores the copy you already have in docker/vendor/. Without it the image still
# builds and runs, but the Vietnam tools report that vnstock is missing (crypto, world markets, news work).
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin finhub \
    && mkdir /data && chown finhub:finhub /data

WORKDIR /app

COPY requirements.txt docker/requirements-vnstock-deps.txt ./
RUN pip install -r requirements.txt -r requirements-vnstock-deps.txt

# Optional vendored vnstock/vnai (see above). The directory always exists (it holds a .gitkeep).
COPY docker/vendor/ /tmp/vendor/
RUN if [ -f /tmp/vendor/vnstock-site-packages.tar.gz ]; then \
        tar -xzf /tmp/vendor/vnstock-site-packages.tar.gz -C "$(python -c 'import site; print(site.getsitepackages()[0])')" \
        && echo "vnstock: vendored copy installed"; \
    else \
        echo "vnstock: NOT included (Vietnam tools will be unavailable)"; \
    fi \
    && rm -rf /tmp/vendor

COPY server.py alert_daemon.py config.json ./
COPY finhub_mcp ./finhub_mcp

# Alerts live in a volume so they survive container rebuilds and are shared by the server and the daemon.
ENV FINHUB_ALERT_STORE=/data/alerts.json
VOLUME ["/data"]

USER finhub
EXPOSE 8000

# Default command: the HTTP MCP server. HTTP mode requires FINHUB_HTTP_TOKEN (the server refuses to start without it).
CMD ["python", "server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1
