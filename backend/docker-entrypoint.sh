#!/usr/bin/env sh
# Entrypoint for both roles of the backend image.
#
#   load   - build the SQLite database from the GTFS static feed, then exit.
#            Idempotent: skips when the database already carries the feed,
#            unless GTFS_FORCE_RELOAD=1.
#   serve  - run the API and the realtime poller.
set -eu

DB_PATH="${DB_PATH:-/data/gtfs.db}"
GTFS_STATIC_DIR="${GTFS_STATIC_DIR:-/gtfs}"
PORT="${PORT:-8000}"

already_loaded() {
  [ -f "$DB_PATH" ] || return 1
  python - "$DB_PATH" <<'PY'
import sqlite3, sys
try:
    c = sqlite3.connect(sys.argv[1])
    row = c.execute("SELECT value FROM meta WHERE key='static_loaded_at'").fetchone()
    n = c.execute("SELECT COUNT(*) FROM gtfs_routes").fetchone()[0]
    sys.exit(0 if row and n > 0 else 1)
except Exception:
    sys.exit(1)
PY
}

case "${1:-serve}" in
  load)
    if [ "${GTFS_FORCE_RELOAD:-0}" != "1" ] && already_loaded; then
      echo "[loader] static feed already present in $DB_PATH - skipping."
      exit 0
    fi
    if [ ! -f "$GTFS_STATIC_DIR/routes.txt" ]; then
      echo "[loader] ERROR: no GTFS feed at $GTFS_STATIC_DIR (routes.txt missing)." >&2
      echo "[loader] Check the gtfs bind mount in docker-compose.yml." >&2
      exit 1
    fi
    echo "[loader] loading $GTFS_STATIC_DIR -> $DB_PATH"
    exec python scripts/load_static.py --gtfs-dir "$GTFS_STATIC_DIR" --db "$DB_PATH"
    ;;
  serve)
    if ! already_loaded; then
      echo "[api] WARNING: $DB_PATH has no static feed. Static and analytics" >&2
      echo "[api] endpoints will be empty until the loader service has run." >&2
    fi
    exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
         --proxy-headers --forwarded-allow-ips='*'
    ;;
  *)
    exec "$@"
    ;;
esac
