"""config/dburl.py: DATABASE_URL -> DATABASES["default"] + schema."""

import pytest

from config.dburl import DEFAULT_DATABASE_URL, database_url, parse_database_url


def test_full_url_with_schema():
    db, schema = parse_database_url("postgresql://app:secret@db.example:6543/minty?schema=pettycash_alt")
    assert schema == "pettycash_alt"
    assert db == {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "minty",
        "USER": "app",
        "PASSWORD": "secret",
        "HOST": "db.example",
        "PORT": "6543",
        "OPTIONS": {"options": "-c search_path=pettycash_alt,public"},
    }


def test_schema_defaults_to_pettycashv3():
    db, schema = parse_database_url("postgresql://app@db/minty")
    assert schema == "pettycashv3"
    assert db["OPTIONS"] == {"options": "-c search_path=pettycashv3,public"}


def test_empty_schema_falls_back_to_default():
    _, schema = parse_database_url("postgresql://app@db/minty?schema=")
    assert schema == "pettycashv3"


def test_port_defaults_to_5432():
    db, _ = parse_database_url("postgresql://app@db/minty")
    assert db["PORT"] == "5432"
    assert db["PASSWORD"] == ""


def test_other_query_params_are_kept_and_schema_is_not_passed_on():
    db, schema = parse_database_url(
        "postgresql://app:pw@db/minty?sslmode=require&schema=s1&connect_timeout=5"
    )
    assert schema == "s1"
    assert db["OPTIONS"] == {
        "sslmode": "require",
        "connect_timeout": "5",
        "options": "-c search_path=s1,public",
    }
    assert "schema" not in db["OPTIONS"]


def test_credentials_and_name_are_percent_decoded():
    db, _ = parse_database_url("postgresql://us%40er:p%40ss%2Fw%3Ard@db/my%20db")
    assert db["USER"] == "us@er"
    assert db["PASSWORD"] == "p@ss/w:rd"
    assert db["NAME"] == "my db"


@pytest.mark.parametrize(
    "scheme", ["postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"]
)
def test_accepted_schemes(scheme):
    db, _ = parse_database_url(f"{scheme}://app:pw@db:5432/minty")
    assert db["ENGINE"] == "django.db.backends.postgresql"
    assert db["NAME"] == "minty"


def test_other_schemes_are_refused():
    with pytest.raises(ValueError):
        parse_database_url("mysql://app@db/minty")


def test_database_url_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url() == DEFAULT_DATABASE_URL == "postgresql://postgres@localhost:5432/postgres"
    db, schema = parse_database_url(database_url())
    assert (db["USER"], db["HOST"], db["PORT"], db["NAME"], schema) == (
        "postgres", "localhost", "5432", "postgres", "pettycashv3"
    )


def test_database_url_reads_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x@y/z?schema=q")
    assert database_url() == "postgres://x@y/z?schema=q"
