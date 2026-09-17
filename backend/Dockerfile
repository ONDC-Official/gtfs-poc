# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Service tier: FastAPI + the realtime poller.
# The same image runs the API and the one-shot GTFS static loader; which one
# you get is decided by the command, not by a separate build.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so a source edit does not re-resolve the whole tree.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Defaults point at the paths the compose file mounts. Overridable via env.
ENV DB_PATH=/data/gtfs.db \
    GTFS_STATIC_DIR=/gtfs \
    PORT=8000

# Run as a non-root user; /data is a volume so it must be owned by that user.
RUN useradd --system --uid 10001 --create-home appuser \
 && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
