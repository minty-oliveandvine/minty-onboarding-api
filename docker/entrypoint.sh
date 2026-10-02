#!/usr/bin/env sh
# Container entrypoint for the onboarding Django API.
#
# This service is a TENANT of the schema Flask owns: settings.py pins search_path to
# the ?schema= on DATABASE_URL (default pettycashv3), and shared_models maps tables Flask's
# Alembic migrations create. So wait for both the database and that schema rather than
# creating the schema ourselves, which would race Alembic and let Django win tables it is
# only supposed to read. DATABASE_URL is parsed by config/dburl.py, exactly as settings.py
# parses it, so the two cannot disagree about where the database is.
#
# NOTE: no `manage.py migrate` here, unlike minty-payment-request-api. This service owns zero
# tables and ships zero migrations by design -- running migrate would be a no-op at best
# and, if a migration ever appeared by accident, a schema fight at worst.
set -eu

python - <<'PY'
import os
import time

import psycopg2

from config.dburl import database_url, parse_database_url

db, schema = parse_database_url(database_url())

# Seconds, not attempts: the schema arrives only once Flask has finished its own
# migrations, which on a cold volume takes a while.
deadline = time.monotonic() + int(os.environ.get("DB_WAIT_SECONDS", "180"))
last_error = None

while time.monotonic() < deadline:
    try:
        conn = psycopg2.connect(
            host=db["HOST"],
            port=db["PORT"],
            dbname=db["NAME"],
            user=db["USER"],
            password=db["PASSWORD"],
            **db["OPTIONS"],
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                    [schema],
                )
                if cur.fetchone():
                    break
                last_error = f"schema {schema} does not exist yet"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - any connection failure is a retry
        last_error = exc
    time.sleep(2)
else:
    raise RuntimeError(f"Database/schema not ready: {last_error}")
PY

echo "Starting application command: $*"
exec "$@"
