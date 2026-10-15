"""config/settings refuses to boot with the placeholder SECRET_KEY outside development.

Regression test for the 2026-09-14 production incident: the service ran with no
SECRET_KEY set, fell back to the in-repo placeholder, and 401'd every authenticated call.
APP_ENV decides: ``development`` is DEBUG, anything else (or unset) is production.
"""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured


def _load_settings(monkeypatch, env):
    for k in ("SECRET_KEY", "APP_ENV", "SUBSCRIPTION_API_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # load_dotenv() must not put a real key back from .env for this test.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    sys.modules.pop("config.settings", None)
    return importlib.import_module("config.settings")


def test_placeholder_key_is_refused_in_production(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY is not set"):
        _load_settings(monkeypatch, {"APP_ENV": "production"})


def test_unset_app_env_means_production(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY is not set"):
        _load_settings(monkeypatch, {})


def test_unknown_app_env_means_production(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY is not set"):
        _load_settings(monkeypatch, {"APP_ENV": "staging"})


def test_placeholder_key_is_tolerated_in_development(monkeypatch):
    mod = _load_settings(monkeypatch, {"APP_ENV": "development"})
    assert mod.DEBUG is True
    assert mod.SECRET_KEY == "change-me-in-production"


def test_a_real_key_boots_in_production(monkeypatch):
    mod = _load_settings(monkeypatch, {
        "APP_ENV": "production", "SECRET_KEY": "x" * 64,
        "SUBSCRIPTION_API_URL": "https://subscription.example/",
    })
    assert mod.DEBUG is False
    assert mod.SECRET_KEY == "x" * 64
    assert mod.SUBSCRIPTION_API_URL == "https://subscription.example"


def test_production_refuses_to_boot_without_the_subscription_api(monkeypatch):
    """Cards, consent and finalize's trial start all go there; no silent localhost."""
    with pytest.raises(ImproperlyConfigured, match="SUBSCRIPTION_API_URL is not set"):
        _load_settings(monkeypatch, {"APP_ENV": "production", "SECRET_KEY": "x" * 64})


def test_development_defaults_the_subscription_api_to_localhost(monkeypatch):
    mod = _load_settings(monkeypatch, {"APP_ENV": "development"})
    assert mod.SUBSCRIPTION_API_URL == "http://localhost:8000"


@pytest.fixture(autouse=True)
def _restore_settings_module():
    yield
    # Put the test settings module back for the rest of the suite.
    sys.modules.pop("config.settings", None)
    importlib.import_module("config.settings_test")


def test_cors_defaults_to_the_wizard_origin(monkeypatch):
    for k in ("CORS_ALLOWED_ORIGINS", "ONBOARDING_WEB_URL"):
        monkeypatch.delenv(k, raising=False)
    mod = _load_settings(monkeypatch, {"APP_ENV": "development"})
    assert mod.CORS_ALLOWED_ORIGINS == ["http://localhost:3030"]
    mod = _load_settings(monkeypatch, {"APP_ENV": "development", "ONBOARDING_WEB_URL": "https://onboarding.example/"})
    assert mod.CORS_ALLOWED_ORIGINS == ["https://onboarding.example"]


def test_preview_origins_are_off_unless_set(monkeypatch):
    monkeypatch.delenv("PREVIEW_ORIGIN_REGEX", raising=False)
    assert _load_settings(monkeypatch, {"APP_ENV": "development"}).CORS_ALLOWED_ORIGIN_REGEXES == []
    regex = r"^https://pr-[a-z0-9-]+-onboarding\.dailyminty\.com$"
    mod = _load_settings(monkeypatch, {
        "APP_ENV": "production", "SECRET_KEY": "x" * 64,
        "SUBSCRIPTION_API_URL": "https://subscription.example", "PREVIEW_ORIGIN_REGEX": regex,
    })
    assert mod.CORS_ALLOWED_ORIGIN_REGEXES == [regex]


def test_a_loose_preview_regex_refuses_to_boot(monkeypatch):
    with pytest.raises(ImproperlyConfigured, match="PREVIEW_ORIGIN_REGEX"):
        _load_settings(monkeypatch, {"APP_ENV": "development", "PREVIEW_ORIGIN_REGEX": ".*"})
