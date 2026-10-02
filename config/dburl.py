"""DATABASE_URL -> Django DATABASES entry + schema name.

Plain Python, no Django import: docker/entrypoint.sh imports it before Django is set up,
to wait for the same database and schema settings.py will use.

    postgresql://user:pass@host:5432/dbname?schema=pettycashv3&sslmode=require

``schema`` is popped from the query (default "pettycashv3") and becomes the search_path;
it is not a libpq parameter and is never passed to the driver. Every other query
parameter is kept and lands in OPTIONS (sslmode, connect_timeout, ...). User, password
and database name are percent-decoded, so a password containing ``@`` or ``/`` is written
``%40`` / ``%2F``.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, unquote, urlsplit

DEFAULT_DATABASE_URL = "postgresql://postgres@localhost:5432/postgres"
DEFAULT_SCHEMA = "pettycashv3"

_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}


def database_url() -> str:
    """DATABASE_URL from the environment, or the local default."""
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def parse_database_url(url: str) -> tuple[dict, str]:
    """``(db_dict, schema)`` for ``DATABASES["default"]`` and ``settings.DB_SCHEMA``."""
    parts = urlsplit(url)
    if parts.scheme not in _SCHEMES:
        raise ValueError(
            f"DATABASE_URL scheme {parts.scheme!r} is not supported; use postgresql://"
        )

    options = dict(parse_qsl(parts.query, keep_blank_values=True))
    schema = options.pop("schema", "") or DEFAULT_SCHEMA
    options["options"] = f"-c search_path={schema},public"

    db = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or 5432),
        "OPTIONS": options,
    }
    return db, schema
