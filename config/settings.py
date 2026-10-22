import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from minty_shared.config import (
    DEFAULT_LOCAL_DATABASE_URL,
    app_env,
    database_url_from_env,
    django_database,
    preview_origin_regexes,
    require_secret_key,
)
from minty_shared.logctx import logging_config
from minty_shared.time import DEFAULT_ENTITY_TIMEZONE

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Shared with the Flask app (Petty Cash) and minty-payment-request-api. Flask MINTS
# the JWTs this service verifies, so a mismatch here 401s every request rather
# than failing loudly at boot. All three services must read it from one source.
_DEFAULT_SECRET_KEY = "change-me-in-production"
# development | production. Anything else, or unset, is production (minty_shared.config).
APP_ENV = app_env()
DEBUG = APP_ENV == "development"

# REFUSE TO BOOT WITH THE PLACEHOLDER KEY OUTSIDE DEVELOPMENT. On 2026-09-14 the production
# deployment ran without SECRET_KEY set: every authenticated call answered 401
# "Signature verification failed" while the public endpoints kept working, and nothing
# said why. Worse than broken, it was forgeable -- the fallback string is in the repo.
# A crash at startup is the loud failure this comment used to say we did not have.
try:
    SECRET_KEY = require_secret_key(
        os.environ.get("SECRET_KEY"), placeholder=_DEFAULT_SECRET_KEY, development=DEBUG
    )
except RuntimeError as exc:
    raise ImproperlyConfigured(str(exc)) from None
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

# Deliberately no django.contrib.contenttypes / django.contrib.auth.
#
# Copied from minty-payment-request-api for the same reason, which holds doubly here:
# authentication is our own bearer scheme (core.auth) and authorisation our own
# membership checks (core.permissions). There is no admin, no sessions, no
# ContentType lookups. Installing them only made `migrate` want to CREATE TABLE
# django_content_type / auth_* inside pettycashv3 -- a schema Alembic owns --
# which fails on any database where those tables already exist.
INSTALLED_APPS = [
    "corsheaders",
    "core",
    "shared_models",
    "onboarding",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "minty_shared.django.middleware.RequestLoggingMiddleware",
    "django.middleware.common.CommonMiddleware",
]

# ---------------------------------------------------------------------------
# CORS -- the onboarding wizard is a separate origin (its own Next.js app)
#
# Note what is NOT in CORS_ALLOW_HEADERS: x-entity-id. minty-payment-request-api scopes a
# request to a company through that header; onboarding passes entity_id in the
# query string or body, because its FIRST call (create the entity) has no entity
# to name yet. Advertising a header nothing sends would only invite one to be
# sent and then silently ignored.
# ---------------------------------------------------------------------------
ONBOARDING_WEB_URL = os.environ.get("ONBOARDING_WEB_URL", "http://localhost:3030").rstrip("/")

# Optional override; defaults to the wizard's own origin.
CORS_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in (os.environ.get("CORS_ALLOWED_ORIGINS") or ONBOARDING_WEB_URL).split(",")
    if origin.strip()
]
# A branch's preview set (minty_shared.config); unset = off.
try:
    CORS_ALLOWED_ORIGIN_REGEXES = preview_origin_regexes(os.environ.get("PREVIEW_ORIGIN_REGEX"))
except ValueError as exc:
    raise ImproperlyConfigured(str(exc)) from None
CORS_ALLOW_HEADERS = [
    "authorization",
    "content-type",
    "accept",
    "origin",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database -- the same database and schema the Flask app owns
#
# This service is a TENANT of pettycashv3, never its owner. Every model is
# managed = False and this repo ships no migrations: Alembic in Minty is the
# owner-of-record for all DDL. A new column means an Alembic migration there
# first, then a hand-edit of the model here.
# ---------------------------------------------------------------------------
# One DATABASE_URL, the schema inside it: postgresql://user:pass@host:5432/db?schema=pettycashv3
# (minty_shared.config). DB_SCHEMA is the schema every model lives in, shared with Minty, which
# reads the same URL the same way. pettycashv3 is the permanent production name and the
# default; it is a setting, not a literal - every db_table is unqualified and resolves through
# search_path, and the raw queries read this. The test settings and the root conftest read it
# too, so `?schema=pettycash_alt` on the test URL proves it.
_DEFAULT_DB, DB_SCHEMA = django_database(database_url_from_env(DEFAULT_LOCAL_DATABASE_URL))

DATABASES = {"default": _DEFAULT_DB}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
USE_I18N = False
USE_TZ = True

# UTC for storage, Hong Kong for the one thing that is a display decision:
# GET /api/onboarding/server-time answers "what is today" so the wizard's date
# picker caps at the user's local midnight rather than the browser's. Every backend's
# default zone is minty_shared.time's, so the services agree about what day it is.
TIME_ZONE = "UTC"
DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE") or DEFAULT_ENTITY_TIMEZONE

# ---------------------------------------------------------------------------
# Cross-module: the Flask app and the subscription API
#
# There are no STRIPE_* or XERO_* credentials in this service, and adding any
# would be a bug. Whoever owns the external rail owns the write:
#
#   * Xero rotates refresh tokens on every use and invalidates the previous one,
#     so only ONE service may call /connect/token. That service is Flask.
#     minty-payment-request-api's settings.py makes the same choice for the same reason.
#   * Stripe and the subscription tables have one writer, minty-subscription-api
#     (the subscription engine). A second writer there is a money bug, not a merge
#     conflict.
#
# So anything touching a Xero token or a module grant is a call to Flask, and
# anything touching cards, trials or billing consent is a call to the
# subscription API. core/minty_client.py and core/subscription_client.py are the
# only modules that make those calls, so the rule is enforceable by reading two
# files.
# ---------------------------------------------------------------------------
PETTY_CASH_URL = os.environ.get("PETTY_CASH_URL", "http://localhost:8010").rstrip("/")
# Required outside development: a missing value would send every card, consent and
# finalize call to a localhost that does not exist on the host, and the wizard would
# only ever say "something went wrong".
SUBSCRIPTION_API_URL = os.environ.get("SUBSCRIPTION_API_URL", "").strip().rstrip("/")
if not SUBSCRIPTION_API_URL:
    if not DEBUG:
        raise ImproperlyConfigured("SUBSCRIPTION_API_URL is not set.")
    SUBSCRIPTION_API_URL = "http://localhost:8000"
# Seconds. A constant, not configuration.
MINTY_PROXY_TIMEOUT = 20

# Flask's internal Xero token endpoint, authenticated with the shared SECRET_KEY. Derived
# from PETTY_CASH_URL, the same way minty-payment-request-api derives it, so the two
# services cannot point at different token services.
#
# These were previously read by core/xero_tokens.py via getattr() with a fallback, but were
# never defined here -- so the getattr could only ever return its fallback and the setting
# looked configurable while being nothing of the sort. Defining them is the fix; do not
# re-introduce the getattr.
#
# SECURITY: this endpoint vends live Xero access tokens. It must not be routable from the
# public internet -- restrict it at the ingress.
XERO_TOKEN_SERVICE_URL = f"{PETTY_CASH_URL}/api/internal/xero/token"
XERO_TOKEN_SERVICE_TIMEOUT = 15

# ---------------------------------------------------------------------------
# Logging - one line format for every Minty backend (minty_shared.logctx):
#   <UTC>Z LEVEL [trace=…] [user=…|entity=…|step=…] message
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"

LOGGING = logging_config(
    {"minty-onboarding": "core.log", "minty-onboarding.http": "api.log"},
    log_dir=LOG_DIR,
    no_propagate=["minty-onboarding.http"],
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
# The access line's logger (minty_shared.django.middleware.RequestLoggingMiddleware).
MINTY_HTTP_LOGGER = "minty-onboarding.http"
