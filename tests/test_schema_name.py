"""The schema name is a setting: ``config.settings.DB_SCHEMA``, taken from ``?schema=`` on
DATABASE_URL (the same URL, read the same way, as Minty). No application string may carry
it; ``search_path`` and the raw queries read the setting. Comments and docstrings are free
to say it. The one literal is the default in minty_shared.config."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from minty_shared.config import django_database
from minty_shared.testing import guards

ROOT = Path(__file__).resolve().parents[1]


def test_no_application_string_carries_the_schema_name():
    hits = guards.string_literals(ROOT, "pettycashv3", skip=("tests/", "migrations/"))
    assert hits == [], "read settings.DB_SCHEMA instead of spelling the schema"


ALT_URL = "postgresql://postgres@localhost:5432/postgres?schema=pettycash_alt"


def test_search_path_follows_the_url():
    db, schema = django_database(ALT_URL)
    assert schema == "pettycash_alt"
    assert db["OPTIONS"]["options"] == "-c search_path=pettycash_alt,public"


def test_settings_take_the_schema_from_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", ALT_URL)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    sys.modules.pop("config.settings", None)
    try:
        mod = importlib.import_module("config.settings")
        assert mod.DB_SCHEMA == "pettycash_alt"
        assert mod.DB_SCHEMA in mod.DATABASES["default"]["OPTIONS"]["options"]
    finally:
        # Put the test settings module back for the rest of the suite.
        sys.modules.pop("config.settings", None)
        importlib.import_module("config.settings_test")


def test_search_path_follows_the_setting():
    """Postgres test mode only: the live test database's search_path is DB_SCHEMA."""
    from django.conf import settings

    if settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"):
        pytest.skip("SQLite test database has no search_path (set MINTY_TEST_PG_URI)")
    assert settings.DB_SCHEMA in settings.DATABASES["default"]["OPTIONS"]["options"]
