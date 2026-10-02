"""Fixtures for the onboarding API suite.

Runs against SQLite with ``--no-migrations`` (see pytest.ini). The shared tables are
Alembic's in production, so ``shared_models/apps.py`` flips ``managed`` on when
``SHARED_MODELS_MANAGED_FOR_TESTING`` is set and pytest-django builds them from the
models instead.

WHAT THESE TESTS CAN AND CANNOT PROVE

They prove the service's own logic: auth decisions, response shapes, the bundle/single
split, the money arithmetic, the status codes. They cannot prove the model mirrors match
the real Postgres schema -- SQLite builds tables FROM the models, so a mirror with a
wrong column name builds happily here and fails only against Postgres. That gap is
covered by ``scripts/parity.py``, which runs both services against the real database and
compares them. Both are needed; neither substitutes for the other.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from django.conf import settings
from django.test import Client

from shared_models.models import (BillingPlan, BillingPolicy, CountryInfo,
                                  CurrencyInfo, Entity, EntityFunction,
                                  EntityFunctionMap, User, UserEntity)


def make_token(user_id, *, scope="onboarding", minutes=60, key=None):
    """A JWT shaped exactly like the one Flask mints.

    Kept as a helper rather than a fixture because half the auth tests need to vary one
    claim, and the point of those tests is that varying it changes the answer.
    """
    return jwt.encode(
        {
            "user_id": str(user_id),
            "scope": scope,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
            "iat": datetime.now(timezone.utc),
        },
        key or settings.SECRET_KEY,
        algorithm="HS256",
    )


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def auth(user):
    """Headers for an authenticated request as ``user``."""
    return {"HTTP_AUTHORIZATION": f"Bearer {make_token(user.id)}"}


@pytest.fixture
def user(db):
    return User.objects.create(
        id=str(uuid.uuid4()),
        email="wizard@example.com",
        password="not-checked-here",
        first_name="Wiz",
        last_name="Ard",
        username="wizard@example.com",
        system_role="normal",
        approved=True,
    )


@pytest.fixture
def other_user(db):
    """Somebody with no role on the entity under test -- the 403 case."""
    return User.objects.create(
        id=str(uuid.uuid4()),
        email="stranger@example.com",
        password="not-checked-here",
        first_name="No",
        last_name="Access",
        username="stranger@example.com",
    )


@pytest.fixture
def currencies(db):
    """HKD with a symbol, JPY as the zero-decimal case.

    JPY carries ``decimal_places=0`` here even though the dev database has 2 for every
    row. That is on purpose: the dev data cannot exercise the branch, and dividing minor
    units by a hardcoded 100 is exactly the bug the column exists to prevent.
    """
    hkd = CurrencyInfo.objects.create(
        id=uuid.uuid4(), currency_code="HKD", currency_name="Hong Kong Dollar",
        symbol="HK$", decimal_places=2, is_active=True,
    )
    jpy = CurrencyInfo.objects.create(
        id=uuid.uuid4(), currency_code="JPY", currency_name="Japanese Yen",
        symbol="", decimal_places=0, is_active=True,
    )
    CurrencyInfo.objects.create(
        id=uuid.uuid4(), currency_code="ZWL", currency_name="Zimbabwe Dollar",
        symbol="Z$", decimal_places=2, is_active=False,
    )
    return {"HKD": hkd, "JPY": jpy}


@pytest.fixture
def countries(db):
    """The country registry rows the tests name. ``get_or_create`` so a test that also uses
    the ``entity`` fixture (which needs HK to satisfy ``fk_entities_country`` on Postgres)
    does not insert HK twice."""
    for code, alpha3, name, active, order in (
        ("HK", "HKG", "Hong Kong", True, 1),
        ("AU", "AUS", "Australia", True, 999),
        ("AF", "AFG", "Afghanistan", True, 999),
        ("XX", "XXX", "Nowhere", False, 999),
    ):
        CountryInfo.objects.get_or_create(
            country_code=code,
            defaults={"alpha3_code": alpha3, "country_name_en": name,
                      "is_active": active, "display_order": order},
        )


@pytest.fixture
def modules(db):
    """The two canonical module catalog rows."""
    return {
        "PETTY_CASH": EntityFunction.objects.create(
            id=str(uuid.uuid4()), function_code="PETTY_CASH",
            function_name="Petty Cash", is_active=True,
        ),
        "PAYMENT_REQUEST": EntityFunction.objects.create(
            id=str(uuid.uuid4()), function_code="PAYMENT_REQUEST",
            function_name="Payment Request", is_active=True,
        ),
    }


@pytest.fixture
def plans(db, currencies):
    """Two singles and a bundle, priced as the real catalog is.

    28000 + 28000 minor units standalone, 40000 as the bundle -- so the bundle really is
    cheaper than the sum, which is what makes the discount assertions meaningful.
    """
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="PETTY_CASH", display_name="Petty Cash",
        amount=28000, currency="HKD", interval_months=1, is_active=True,
    )
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="BILL", display_name="Payment Request",
        amount=28000, currency="HKD", interval_months=1, is_active=True,
    )
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="BILL+PETTY_CASH", display_name="Super Minty",
        amount=40000, currency="HKD", interval_months=1, is_active=True,
    )


@pytest.fixture
def policy(db):
    return BillingPolicy.objects.create(
        id=1, trial_days=30, paid_cancel_access_days=30,
        past_due_window_days=15, retry_offsets_days="1,2,3",
    )


@pytest.fixture
def entity(db, user, countries):
    """An in-progress onboarding entity the ``user`` fixture is a member of.

    ``countries`` because ``entities.country_code`` is a real FK on Postgres."""
    e = Entity.objects.create(
        id=str(uuid.uuid4()), name="Wizard Trading Co",
        country_code="HK", status="onboarding",
    )
    UserEntity.objects.create(
        user_id=user.id, entity_id=e.id, role="super_admin", approved=True
    )
    return e


@pytest.fixture
def enable_module(db, modules):
    """Callable: turn a module on for an entity, the way subscription state would."""

    def _enable(entity, code, on=True):
        # created_at / updated_at are NOT NULL with no database default in the real schema,
        # so the fixture sets them. Leaving them out passed until the mirror declared them,
        # which is the whole hazard of a partial mirror: the test database is built FROM the
        # models, so it only enforces what the models happen to say.
        now = datetime.now(timezone.utc)
        return EntityFunctionMap.objects.create(
            entity_id=entity.id,
            entity_function_id=modules[code].id,
            is_enabled=on,
            enabled_at=now if on else None,
            disabled_at=None if on else now,
            created_by=None,  # a person or NULL (schema section 4); a test fixture is nobody
            created_at=now,
            updated_at=now,
        )

    return _enable


# ---------------------------------------------------------------------------------------
# NO NETWORK. Every outbound call in this service goes through `requests` -- the Flask
# proxy in core/minty_client and the Xero token path in core/xero_tokens -- and none of
# it may reach a real host from a test.
#
# This is not hygiene. Several state tests set `xero_org_id` without stubbing the Xero
# path, so before this guard `access_token_for` really POSTed to XERO_TOKEN_SERVICE_URL.
# With nothing on that port the call failed fast and read as "could not verify"; with a
# dev Flask running it reached a real endpoint, and the suite's answer depended on what
# was listening on the Petty Cash port. A test whose result depends on the developer's other
# terminals is not a test.
#
# Tests that need a response stub `requests.request` / `requests.post` / `requests.get`
# themselves (see test_minty_client.py, test_xero_tokens.py); this fixture makes the
# unstubbed case loud.
# ---------------------------------------------------------------------------------------


class _NetworkBlocked(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import requests

    def _refuse(*args, **kwargs):
        raise _NetworkBlocked(
            "a test tried to make a real HTTP call; stub requests.request / .post / .get"
        )

    # `requests.request` is what minty_client uses; the verb helpers are what xero_tokens
    # uses. Patching the Session would miss the module-level helpers, so all three are
    # patched at the names the code actually calls.
    monkeypatch.setattr(requests, "request", _refuse)
    monkeypatch.setattr(requests, "post", _refuse)
    monkeypatch.setattr(requests, "get", _refuse)
