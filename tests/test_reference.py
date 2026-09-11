"""Group A -- reference data and the price catalog.

The catalog tests carry the weight here. The three registry endpoints are thin, but
``/plans`` does real arithmetic on money in minor units, and every assertion below
corresponds to a rule Flask's version documents as having once been wrong.
"""

import uuid

import pytest

from shared_models.models import BillingPlan, BillingPolicy

SERVER_TIME = "/api/onboarding/server-time"
CURRENCIES = "/api/onboarding/currencies"
COUNTRIES = "/api/onboarding/countries"
PLANS = "/api/onboarding/plans"


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_server_time_is_an_iso_date(client):
    resp = client.get(SERVER_TIME)
    assert resp.status_code == 200
    from datetime import date

    date.fromisoformat(resp.json()["today"])  # raises if not YYYY-MM-DD


@pytest.mark.django_db
def test_server_time_uses_the_display_timezone_not_utc(client, settings):
    """"Today" must be the user's today, not the server's.

    The wizard's date picker caps selectable dates at this value so a future date cannot
    be chosen. With Asia/Hong_Kong being UTC+8, a request in the UTC evening is already
    tomorrow in Hong Kong -- if this answered UTC, the picker would refuse a date the
    user is legitimately living in.

    Asserted by comparison rather than against a hardcoded date, so the test does not
    start failing at a particular hour.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    settings.DISPLAY_TIMEZONE = "Asia/Hong_Kong"
    resp = client.get(SERVER_TIME)
    expected = datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    assert resp.json()["today"] == expected


@pytest.mark.django_db
def test_currencies_offers_only_active_rows(client, currencies):
    codes = {c["iso_code"] for c in client.get(CURRENCIES).json()["currencies"]}
    assert codes == {"HKD", "JPY"}
    assert "ZWL" not in codes  # is_active=False


@pytest.mark.django_db
def test_currency_id_is_a_string_not_a_uuid_object(client, currencies):
    """THE UUID TRAP.

    ``currency_info.id`` is a Postgres uuid. Flask hands back a ``str``; Django's
    UUIDField hands back a ``UUID`` object. The wizard SUBMITS this value straight back
    as the entity's ``currency_id``, so if it serialises differently the round trip
    breaks at Step 1 -- and it breaks silently, as a create that cannot resolve the
    currency rather than as a type error here.
    """
    rows = client.get(CURRENCIES).json()["currencies"]
    for row in rows:
        assert isinstance(row["currency_id"], str)
        uuid.UUID(row["currency_id"])  # parses as a real uuid


@pytest.mark.django_db
def test_currencies_are_ordered_by_name(client, currencies):
    names = [c["currency_name"] for c in client.get(CURRENCIES).json()["currencies"]]
    assert names == sorted(names)


@pytest.mark.django_db
def test_countries_order_by_display_order_then_name(client, countries):
    """display_order floats common countries above the alphabetical tail.

    Hong Kong carries display_order=1, so it must come before Afghanistan despite
    losing on the alphabet. Ties fall back to name, which is what every row does while
    the 999 default stands.
    """
    rows = client.get(COUNTRIES).json()["countries"]
    codes = [c["country_code"] for c in rows]
    assert codes[0] == "HK"
    assert codes[1:] == ["AF", "AU"]
    assert "XX" not in codes  # is_active=False


@pytest.mark.django_db
def test_country_id_duplicates_the_code(client, countries):
    """The PK is the alpha-2 code, so both keys carry it.

    Redundant, and kept: the wizard's submit-the-id contract needs no change. Renaming
    would be tidier and would break Step 1.
    """
    for row in client.get(COUNTRIES).json()["countries"]:
        assert row["country_id"] == row["country_code"]


# ---------------------------------------------------------------------------
# The price catalog
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_plans_lists_singles_and_excludes_the_bundle(client, auth, plans, modules, policy):
    """The bundle is not a shelf item.

    A customer picks modules; the bundle is what those modules cost together. It comes
    back as bundle_amount / bundle_codes instead, and a bundle row leaking into ``plans``
    would render as a third selectable product.
    """
    body = client.get(PLANS, **auth).json()
    assert [p["code"] for p in body["plans"]] == ["PETTY_CASH", "BILL"]
    assert body["bundle_codes"] == ["BILL", "PETTY_CASH"]


@pytest.mark.django_db
def test_amounts_convert_out_of_minor_units(client, auth, plans, modules, policy):
    """28000 minor units of a 2-decimal currency is 280.00, not 28000."""
    body = client.get(PLANS, **auth).json()
    petty = next(p for p in body["plans"] if p["code"] == "PETTY_CASH")
    assert petty["amount"] == 280.0
    assert petty["formatted_amount"] == "280.00"
    assert body["bundle_amount"] == 400.0


@pytest.mark.django_db
def test_zero_decimal_currency_is_not_divided_by_a_hundred(
    client, auth, currencies, modules, policy
):
    """JPY has no minor unit: 280 minor units IS 280 yen.

    Dividing by a hardcoded 100 would price a module at 2.80 instead of 280 -- a
    hundredfold error in the customer's favour. This is why decimal_places is read from
    currency_info rather than assumed.
    """
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="PETTY_CASH", display_name="Petty Cash",
        amount=280, currency="JPY", interval_months=1, is_active=True,
    )
    body = client.get(PLANS, **auth).json()
    petty = next(p for p in body["plans"] if p["code"] == "PETTY_CASH")
    assert petty["amount"] == 280.0
    assert petty["formatted_amount"] == "280"


@pytest.mark.django_db
def test_the_bundle_is_cheaper_than_the_sum(client, auth, plans, modules, policy):
    """The bundle IS the discount -- there is no separate discount field.

    Pins the relationship the wizard's Step 2 summary renders. If a future catalog made
    the bundle cost more than the parts, the summary would be advertising a discount that
    charges extra.
    """
    body = client.get(PLANS, **auth).json()
    assert body["bundle_amount"] < sum(p["amount"] for p in body["plans"])


@pytest.mark.django_db
def test_symbol_comes_from_the_database(client, auth, plans, modules, policy):
    body = client.get(PLANS, **auth).json()
    assert all(p["currency_symbol"] == "HK$" for p in body["plans"])


@pytest.mark.django_db
def test_symbol_falls_back_to_the_code_when_unset(
    client, auth, currencies, modules, policy
):
    """The dev database has an empty symbol on EVERY row, so this is the live path.

    Flask behaves identically, which is why /plans currently answers "HKD" rather than
    "HK$" against the real database. A missing symbol must never cost the amount, so the
    fallback is the code, not an empty string.
    """
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="BILL", display_name="Payment Request",
        amount=28000, currency="JPY", interval_months=1, is_active=True,
    )
    body = client.get(PLANS, **auth).json()
    bill = next(p for p in body["plans"] if p["code"] == "BILL")
    assert bill["currency_symbol"] == "JPY"


@pytest.mark.django_db
def test_a_module_with_no_plan_is_omitted_not_free(client, auth, modules, policy, currencies):
    """A half-seeded catalog must not invent a price.

    Only PETTY_CASH gets a row. BILL must be absent from the list, not present at zero --
    a module rendered at 0.00 reads as free.
    """
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="PETTY_CASH", display_name="Petty Cash",
        amount=28000, currency="HKD", interval_months=1, is_active=True,
    )
    body = client.get(PLANS, **auth).json()
    assert [p["code"] for p in body["plans"]] == ["PETTY_CASH"]


@pytest.mark.django_db
def test_inactive_plans_are_not_sold(client, auth, modules, policy, currencies):
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="PETTY_CASH", display_name="Petty Cash",
        amount=28000, currency="HKD", interval_months=1, is_active=False,
    )
    body = client.get(PLANS, **auth).json()
    assert body["plans"] == []


@pytest.mark.django_db
def test_display_name_comes_from_the_module_catalog(client, auth, plans, modules, policy):
    """``entity_function.function_name`` names the module in the app.

    Deliberately not ``billing_plan.display_name``, which is what a customer reads on an
    invoice and is allowed to diverge -- "Petty Cash" in the UI, "Petty Cash (Monthly)"
    on a bill. The bundle row here is named "Super Minty" and that name must not leak
    into a module label.
    """
    body = client.get(PLANS, **auth).json()
    names = {p["code"]: p["name"] for p in body["plans"]}
    assert names == {"PETTY_CASH": "Petty Cash", "BILL": "Payment Request"}


@pytest.mark.django_db
def test_trial_days_comes_from_policy(client, auth, plans, modules, policy):
    policy.trial_days = 14
    policy.save()
    assert client.get(PLANS, **auth).json()["trial_period_days"] == 14


@pytest.mark.django_db
def test_missing_policy_row_falls_back_to_the_shipped_default(
    client, auth, plans, modules
):
    """No billing_policy row -- a database predating the migration, or a fresh test DB.

    Quiet, not an error: the default IS the shipped behaviour. Answering 0 would tell the
    customer there is no trial.
    """
    BillingPolicy.objects.all().delete()
    assert client.get(PLANS, **auth).json()["trial_period_days"] == 30


@pytest.mark.django_db
def test_interval_translates_months_to_a_vocabulary(client, auth, modules, policy, currencies):
    """12 months is a year. Stored as a number so a yearly plan needs no new column."""
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="PETTY_CASH", display_name="Petty Cash",
        amount=28000, currency="HKD", interval_months=12, is_active=True,
    )
    body = client.get(PLANS, **auth).json()
    assert body["plans"][0]["billing_interval"] == "year"


@pytest.mark.django_db
def test_empty_catalog_answers_a_usable_shape(client, auth, modules, policy):
    """No plans at all must not 500, and must not omit keys.

    The wizard reads bundle_amount and trial_period_days unconditionally. Absent keys
    crash Step 2 where zero values merely render an empty summary -- and module selection
    has to keep working, because the trial is card-free and nothing is being charged yet.
    """
    body = client.get(PLANS, **auth).json()
    assert body == {
        "plans": [],
        "bundle_amount": 0.0,
        "bundle_codes": [],
        "bundle_currency": None,
        "trial_period_days": 30,
    }


@pytest.mark.django_db
def test_largest_bundle_wins(client, auth, plans, modules, policy):
    """A future three-module bundle must beat a two-module one, not depend on row order."""
    BillingPlan.objects.create(
        id=uuid.uuid4(), code="BILL+PETTY_CASH+PAYROLL", display_name="Everything",
        amount=50000, currency="HKD", interval_months=1, is_active=True,
    )
    body = client.get(PLANS, **auth).json()
    assert body["bundle_codes"] == ["BILL", "PAYROLL", "PETTY_CASH"]
    assert body["bundle_amount"] == 500.0
