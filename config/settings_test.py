import os

# Before the import: settings.py refuses the placeholder SECRET_KEY outside development, and
# APP_ENV defaults to production. The fixed key below is set after the import, too late for
# that check.
os.environ.setdefault("APP_ENV", "development")

from config.dburl import parse_database_url  # noqa: E402
from config.settings import *  # noqa: E402, F401, F403

# A fixed key so tests can mint a JWT the service will accept. Nothing about the
# value matters except that signing and verifying use the same one -- which is the
# whole contract with Flask in production, too.
SECRET_KEY = "test-secret-key-shared-with-flask"

# Two test databases, chosen by MINTY_TEST_PG_URI:
#
#   unset  -> SQLite in memory, tables built FROM THE MODELS (SHARED_MODELS_MANAGED_FOR_TESTING).
#             Proves this service's logic. Cannot see whether the models match the real schema.
#   set    -> PostgreSQL, a database built FROM docs/schema/01_schema_rebased.sql in the Minty repo
#             by tests/pg_harness.py (loaded by conftest.py at the repo root). Nothing is created
#             from the models; a mirror column the schema lacks fails on the SELECT, which is the
#             point. Same knobs as Minty: MINTY_TEST_PG_DBNAME (minty_test), MINTY_TEST_PG_KEEP=1,
#             PG_BIN, MINTY_REPO (C:\Github\Minty). The URI may carry ?schema= like DATABASE_URL
#             (`...?schema=pettycash_alt` proves nothing hardcodes the schema name).
_PG_URI = os.environ.get("MINTY_TEST_PG_URI")
if _PG_URI:
    _db, DB_SCHEMA = parse_database_url(_PG_URI)
    _db["USER"] = _db["USER"] or "postgres"
    _db["HOST"] = _db["HOST"] or "localhost"
    _db["NAME"] = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    # Never let pytest-django create/destroy a database of its own here; the
    # root conftest overrides django_db_setup and hands it the harness's build.
    _db["TEST"] = {"NAME": _db["NAME"]}
    DATABASES = {"default": _db}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

LOGGING["handlers"]["file_core"] = {"class": "logging.NullHandler"}  # noqa: F405
LOGGING["handlers"]["file_api"] = {"class": "logging.NullHandler"}  # noqa: F405

# Build the shared tables in the test database. In production they are Alembic's.
SHARED_MODELS_MANAGED_FOR_TESTING = not _PG_URI  # tables come from the schema file on Postgres

# No test may reach the real Flask app. Proxy tests stub the transport; this value
# exists so an un-stubbed call fails fast against an obviously fake host instead of
# quietly hitting a developer localhost.
PETTY_CASH_URL = "http://flask.invalid"
XERO_TOKEN_SERVICE_URL = f"{PETTY_CASH_URL}/api/internal/xero/token"
