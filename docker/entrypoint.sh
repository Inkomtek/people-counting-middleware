#!/bin/sh
set -e

# Wait until PostgreSQL accepts connections.
python - <<'PY'
import os, time
import psycopg

for attempt in range(60):
    try:
        psycopg.connect(
            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
            host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"), connect_timeout=3,
        ).close()
        break
    except psycopg.OperationalError:
        print("Waiting for database...", flush=True)
        time.sleep(2)
else:
    raise SystemExit("Database not reachable")
PY

# Only the web container migrates and collects static files, so the scheduler never races it.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
fi

exec "$@"
