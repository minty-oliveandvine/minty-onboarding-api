"""Group D -- petty-cash configuration.

D1 (sales methods, opening balance) is tested as a port. D2 is tested as a PROXY: the
assertions are about what gets forwarded and what comes back, not about Xero, because Xero is
Flask's business.

The opening balance is saved on ``entity_pettycash_settings`` (with the start date) and no
report is written: the first report reads the two values when it is opened.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from core import minty_client
from core.exceptions import UpstreamError
from shared_models.models import (
    AccountInfo,
    Entity,
    EntityPettycashSettings,
    EntitySaleSetting,
    Report,
    SaleInfo,
    UserEntity,
)
from tests.conftest import make_token

SALES = "/api/onboarding/sales-methods"
OPENING = "/api/onboarding/opening-balance"
ACCOUNT_CODES = "/api/onboarding/account-codes"
CONTACTS_CREATE = "/api/onboarding/contacts/create"
BILL_CODES = "/api/onboarding/bill-codes"


def post_json(client, auth, url, payload):
    return client.post(url, payload, content_type="application/json", **auth)


def set_methods(client, auth, entity, electronic=(), delivery=()):
    return post_json(
        client, auth, SALES,
        {
            "entity_id": entity.id,
            "electronic": list(electronic),
            "delivery": list(delivery),
        },
    )


def get_methods(client, auth, entity):
    return client.get(SALES, {"entity_id": entity.id}, **auth).json()


# ---------------------------------------------------------------------------
# Sales methods -- reconciliation
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_setting_methods_returns_what_was_applied(client, auth, entity):
    resp = set_methods(client, auth, entity, electronic=["Visa", "Octopus"], delivery=["Keeta"])
    assert resp.status_code == 200
    assert resp.json() == {"electronic": ["Visa", "Octopus"], "delivery": ["Keeta"]}


@pytest.mark.django_db
def test_methods_round_trip(client, auth, entity):
    set_methods(client, auth, entity, electronic=["Visa", "Octopus"], delivery=["Keeta"])
    assert get_methods(client, auth, entity) == {
        "electronic": ["Visa", "Octopus"],
        "delivery": ["Keeta"],
    }


@pytest.mark.django_db
def test_order_follows_the_submitted_list(client, auth, entity):
    """display_order is assigned 1..n in submitted order, and the GET sorts by it."""
    set_methods(client, auth, entity, electronic=["Octopus", "Visa", "Amex"])
    assert get_methods(client, auth, entity)["electronic"] == ["Octopus", "Visa", "Amex"]


@pytest.mark.django_db
def test_reordering_updates_rather_than_reinserting(client, auth, entity):
    set_methods(client, auth, entity, electronic=["Visa", "Octopus"])
    ids_before = set(
        EntitySaleSetting.objects.filter(entity_id=entity.id).values_list("sale_id", flat=True)
    )
    set_methods(client, auth, entity, electronic=["Octopus", "Visa"])
    ids_after = set(
        EntitySaleSetting.objects.filter(entity_id=entity.id).values_list("sale_id", flat=True)
    )
    assert ids_before == ids_after
    assert get_methods(client, auth, entity)["electronic"] == ["Octopus", "Visa"]


@pytest.mark.django_db
def test_a_removed_method_is_soft_disabled_not_deleted(client, auth, entity):
    """NEVER DELETED. ``report_sale_detail`` rows still reference the catalog row, so a
    method a shop stops accepting must not take its history with it."""
    set_methods(client, auth, entity, electronic=["Visa", "Octopus"])
    set_methods(client, auth, entity, electronic=["Visa"])

    octopus = EntitySaleSetting.objects.get(entity_id=entity.id, sale__sale_name="Octopus")
    assert octopus.is_active is False
    assert get_methods(client, auth, entity)["electronic"] == ["Visa"]


@pytest.mark.django_db
def test_re_adding_a_disabled_method_revives_the_same_row(client, auth, entity):
    set_methods(client, auth, entity, electronic=["Visa"])
    sale_id = EntitySaleSetting.objects.get(entity_id=entity.id, sale__sale_name="Visa").sale_id
    set_methods(client, auth, entity, electronic=[])
    set_methods(client, auth, entity, electronic=["Visa"])
    revived = EntitySaleSetting.objects.get(entity_id=entity.id, sale__sale_name="Visa")
    assert revived.sale_id == sale_id
    assert revived.is_active is True


@pytest.mark.django_db
def test_a_rename_of_capitalisation_keeps_the_link_and_the_catalogue_row(client, auth, entity):
    """Matching is case-insensitive: "VISA" is the catalogue's "Visa", so the company's link
    is kept (and with it the form-field name its historical figures are keyed by)."""
    SaleInfo.objects.create(
        id=uuid.uuid4(), sale_name="Visa", type="electronic", value_name="visa_sales",
        enabled=True, display_order=1,
    )
    set_methods(client, auth, entity, electronic=["Visa"])
    row = EntitySaleSetting.objects.get(entity_id=entity.id)

    set_methods(client, auth, entity, electronic=["VISA"])
    after = EntitySaleSetting.objects.get(entity_id=entity.id)
    assert after.sale_id == row.sale_id
    assert after.sale.sale_name == "Visa", "the catalogue keeps its spelling; nothing was renamed"
    assert after.sale.value_name == "visa_sales"


@pytest.mark.django_db
def test_duplicate_names_are_collapsed_first_spelling_winning(client, auth, entity):
    resp = set_methods(client, auth, entity, electronic=["Visa", "VISA", "visa"])
    assert resp.json()["electronic"] == ["Visa"]
    assert EntitySaleSetting.objects.filter(entity_id=entity.id).count() == 1


@pytest.mark.django_db
def test_blank_names_are_dropped(client, auth, entity):
    resp = set_methods(client, auth, entity, electronic=["Visa", "", "   "])
    assert resp.json()["electronic"] == ["Visa"]


@pytest.mark.django_db
def test_a_non_list_is_rejected(client, auth, entity):
    resp = post_json(
        client, auth, SALES,
        {"entity_id": entity.id, "electronic": "Visa", "delivery": []},
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_cash_is_untouched_by_this_screen(client, auth, entity):
    """'Cash' is not a managed type here.

    It is seeded at entity creation, it is the only figure in the closing-balance formula,
    and it publishes to Xero against its own account -- so submitting an empty Electronic
    list must not disable it.
    """
    cash = SaleInfo.objects.create(
        id=uuid.uuid4(), sale_name="Cash", type="other", value_name="cash_sales",
        enabled=True, display_order=0,
    )
    EntitySaleSetting.objects.create(entity_id=entity.id, sale=cash, is_active=True, display_order=0)
    set_methods(client, auth, entity, electronic=[], delivery=[])
    assert EntitySaleSetting.objects.get(entity_id=entity.id, sale=cash).is_active is True


# ---------------------------------------------------------------------------
# The catalogue row a typed name gets
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_user_typed_method_gets_a_catalogue_row(client, auth, entity):
    """WITHOUT ONE IT IS NOT STORABLE: a report's figures are ``report_sale`` rows pointing at
    the catalogue. The row is global - the next company that types "Tap & Go" links to it."""
    set_methods(client, auth, entity, electronic=["Tap & Go"])
    row = EntitySaleSetting.objects.get(entity_id=entity.id)
    catalog = row.sale
    assert catalog.sale_name == "Tap & Go"
    assert catalog.type == "electronic"
    assert catalog.value_name == "tap_&_go_sales"  # the form-field name Minty derives too


@pytest.mark.django_db
def test_the_same_custom_name_does_not_mint_a_second_catalogue_row(client, auth, entity):
    set_methods(client, auth, entity, electronic=["Tap & Go"])
    set_methods(client, auth, entity, electronic=[])
    set_methods(client, auth, entity, electronic=["tap & go"])
    assert SaleInfo.objects.filter(sale_name__iexact="Tap & Go").count() == 1
    assert EntitySaleSetting.objects.filter(entity_id=entity.id).count() == 1


@pytest.mark.django_db
def test_an_existing_catalogue_row_is_linked_rather_than_duplicated(client, auth, entity):
    SaleInfo.objects.create(
        id=uuid.uuid4(), sale_name="Octopus", type="electronic", value_name="octopus_sales",
        enabled=True, display_order=7,
    )
    set_methods(client, auth, entity, electronic=["octopus"])
    row = EntitySaleSetting.objects.get(entity_id=entity.id)
    assert row.sale.value_name == "octopus_sales"
    assert SaleInfo.objects.filter(sale_name__iexact="octopus").count() == 1


@pytest.mark.django_db
def test_two_companies_share_one_catalogue_row(client, auth, user, entity, countries):
    """The catalogue is global (sale_name is unique); each company's on/off is its own."""
    other = Entity.objects.create(id=str(uuid.uuid4()), name="Other Co", country_code="HK", status="onboarding")
    UserEntity.objects.create(user_id=user.id, entity_id=other.id, role="super_admin", approved=True)

    set_methods(client, auth, entity, electronic=["Payme"])
    set_methods(client, auth, other, electronic=["payme"])
    assert SaleInfo.objects.filter(sale_name__iexact="payme").count() == 1

    set_methods(client, auth, entity, electronic=[])
    assert EntitySaleSetting.objects.get(entity_id=entity.id).is_active is False
    assert EntitySaleSetting.objects.get(entity_id=other.id).is_active is True


# ---------------------------------------------------------------------------
# Sales methods -- access
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_sales_methods_require_membership(client, other_user, entity):
    resp = client.get(
        SALES, {"entity_id": entity.id},
        HTTP_AUTHORIZATION=f"Bearer {make_token(other_user.id)}",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_a_cashier_may_view_but_not_change_methods(client, user, entity):
    """SALES_METHOD_VIEW is cashier; _CREATE is accountant. A real split, unlike the
    invite list, because changing a method changes what the report records."""
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    auth = {"HTTP_AUTHORIZATION": f"Bearer {make_token(user.id)}"}

    assert client.get(SALES, {"entity_id": entity.id}, **auth).status_code == 200
    resp = set_methods(client, auth, entity, electronic=["Visa"])
    assert resp.status_code == 403
    assert resp.json() == {"error": "Access denied"}


# ---------------------------------------------------------------------------
# Opening balance
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _entity_has_a_currency(request):
    """The opening balance needs a currency (2026-10-13); give the shared entity one."""
    if "entity" not in request.fixturenames or "no_currency" in request.keywords:
        return
    from shared_models.models import CurrencyInfo

    entity = request.getfixturevalue("entity")
    hkd = CurrencyInfo.objects.create(id=uuid.uuid4(), currency_code="HKD", currency_name="Hong Kong Dollar",
                                      symbol="HK$", decimal_places=2, is_active=True)
    Entity.objects.filter(id=entity.id).update(currency_id=hkd.id)


def _settings(entity):
    return EntityPettycashSettings.objects.filter(entity_id=entity.id).first()


@pytest.mark.django_db
def test_opening_balance_lands_on_the_settings_row(client, auth, entity):
    today = date.today()
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": today.isoformat(), "cash_addition": 1500.0},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "opening_date": today.isoformat(),
        "opening_balance": 1500.0,
        "cash_addition": 0.0,
        "adjusted_opening_balance": 1500.0,
    }
    row = _settings(entity)
    assert row.opening_balance == Decimal("1500.00")
    assert row.start_date == today


@pytest.mark.django_db
def test_no_report_is_written(client, auth, entity):
    """The first report reads the settings when it is opened; onboarding seeds no draft."""
    post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(), "cash_addition": 9},
    )
    assert not Report.objects.filter(entity_id=entity.id).exists()


@pytest.mark.django_db
def test_an_existing_settings_row_is_updated_not_replaced(client, auth, entity):
    """Step 6 may have created the row first; its account mapping must survive."""
    account = AccountInfo.objects.create(
        id=uuid.uuid4(), entity_id=entity.id, type="EXPENSE", name="Petty Cash",
        xero_account_id=str(uuid.uuid4()), xero_code="090", status="ACTIVE",
    ).id
    EntityPettycashSettings.objects.create(entity_id=entity.id, pettycash_account_id=account)
    first = date.today() - timedelta(days=5)
    second = date.today() - timedelta(days=2)
    for day, amount in ((first, 100), (second, 250)):
        assert post_json(
            client, auth, OPENING,
            {"entity_id": entity.id, "opening_date": day.isoformat(), "cash_addition": amount},
        ).status_code == 200

    assert EntityPettycashSettings.objects.filter(entity_id=entity.id).count() == 1
    row = _settings(entity)
    assert row.start_date == second
    assert row.opening_balance == Decimal("250.00")
    assert row.pettycash_account_id == account


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["submitted", "published"])
def test_once_a_report_is_posted_it_is_409(client, auth, entity, status):
    """The first report already took its opening from the settings; a change would do nothing."""
    Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, status=status,
        transaction_date=date.today() - timedelta(days=10),
        opening_balance=0.0, cash_addition=0.0,
    )
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(), "cash_addition": 10},
    )
    assert resp.status_code == 409
    assert _settings(entity) is None


@pytest.mark.django_db
def test_a_draft_report_does_not_block_a_save(client, auth, entity):
    """Only a POSTED report ends onboarding; an entity onboarded before the settings
    columns were used still has its seeded draft."""
    Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, status="draft",
        transaction_date=date.today() - timedelta(days=3),
        opening_balance=5.0, cash_addition=0.0,
    )
    body = {"entity_id": entity.id, "opening_date": date.today().isoformat(), "cash_addition": 100}
    assert post_json(client, auth, OPENING, body).status_code == 200
    assert post_json(client, auth, OPENING, body).status_code == 200


@pytest.mark.django_db
def test_a_future_date_is_refused(client, auth, entity):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": tomorrow, "cash_addition": 10},
    )
    assert resp.status_code == 400
    assert "future" in resp.json()["error"]
    assert _settings(entity) is None


@pytest.mark.django_db
def test_an_old_past_date_is_allowed(client, auth, entity):
    """Onboarding deliberately has NO seven-day window."""
    old = (date.today() - timedelta(days=25)).isoformat()
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": old, "cash_addition": 10},
    )
    assert resp.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["", None, "13/09/2026", "not-a-date"])
def test_a_bad_date_is_400(client, auth, entity, value):
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": value, "cash_addition": 10},
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_a_negative_amount_is_refused(client, auth, entity):
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(),
         "cash_addition": -5},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Opening amount cannot be negative"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "amount",
    [{"opening_balance": ""}, {"opening_balance": "   "}, {"opening_balance": None},
     {"cash_addition": ""}, {}],
)
def test_a_blank_amount_is_refused_not_saved_as_zero(client, auth, entity, amount):
    """A blank is not 0: the wizard never sends one (it skips the call), so a blank that
    arrives is a missing amount, and saving 0 would open the first report at nothing."""
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(), **amount},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "error": "The opening balance is still empty. Add the amount to continue."
    }
    assert _settings(entity) is None


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["abc", "NaN", "Infinity"])
def test_a_non_number_amount_is_400(client, auth, entity, value):
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(),
         "opening_balance": value},
    )
    assert resp.status_code == 400
    assert _settings(entity) is None


@pytest.mark.django_db
def test_opening_balance_is_accepted_as_the_legacy_amount_key(client, auth, entity):
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "transaction_date": date.today().isoformat(),
         "opening_balance": "42.50"},
    )
    assert resp.status_code == 200
    assert resp.json()["opening_balance"] == 42.5
    assert _settings(entity).opening_balance == Decimal("42.50")


@pytest.mark.django_db
def test_opening_balance_requires_membership(client, other_user, entity):
    resp = client.post(
        OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(),
         "cash_addition": 1},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {make_token(other_user.id)}",
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# D2 -- the proxies. About forwarding, not about Xero.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_proxied_endpoint_forwards_and_relays_the_status(
    client, auth, entity, monkeypatch
):
    """Flask's status reaches the wizard as itself.

    409 here means "connect to Xero first", and the wizard has copy for exactly that. If the
    proxy flattened it into a 200 or a 500 the user would be told the wrong thing.
    """
    seen = {}

    def fake_forward(request, path, *, method="POST", json=None, params=None, base=None):
        seen.update(path=path, method=method, json=json, params=params)
        return {"connected": False, "error": "Connect to Xero first."}, 409

    monkeypatch.setattr(minty_client, "forward", fake_forward)

    resp = client.get(ACCOUNT_CODES, {"entity_id": entity.id}, **auth)
    assert resp.status_code == 409
    assert resp.json()["connected"] is False
    assert seen["path"] == "/api/onboarding/account-codes"
    assert seen["method"] == "GET"
    assert seen["params"] == {"entity_id": entity.id}


@pytest.mark.django_db
def test_a_proxied_post_forwards_the_body_unchanged(client, auth, entity, monkeypatch):
    seen = {}

    def fake_forward(request, path, *, method="POST", json=None, params=None, base=None):
        seen.update(path=path, json=json)
        return {"status": "success"}, 200

    monkeypatch.setattr(minty_client, "forward", fake_forward)

    body = {"entity_id": entity.id, "name": "Some Supplier"}
    resp = post_json(client, auth, CONTACTS_CREATE, body)
    assert resp.status_code == 200
    assert seen["path"] == "/api/onboarding/contacts/create"
    assert seen["json"] == body


@pytest.mark.django_db
def test_an_unreachable_flask_becomes_a_502_with_readable_copy(
    client, auth, entity, monkeypatch
):
    """Not an HTML error page, and not a traceback -- the wizard renders ``error`` in a toast."""
    def boom(request, path, **kwargs):
        raise UpstreamError(minty_client.UNREACHABLE, status=502)

    monkeypatch.setattr(minty_client, "forward", boom)

    resp = client.get(BILL_CODES, {"entity_id": entity.id}, **auth)
    assert resp.status_code == 502
    assert resp.json() == {"error": minty_client.UNREACHABLE}


@pytest.mark.django_db
def test_a_proxied_endpoint_still_needs_a_token(client, entity):
    """The proxy forwards the caller's OWN token, so it must have one to forward.

    This is what stops the service becoming an unauthenticated door into Flask.
    """
    assert client.get(ACCOUNT_CODES, {"entity_id": entity.id}).status_code == 401


@pytest.mark.django_db
def test_a_proxied_endpoint_requires_an_entity_id(client, auth):
    resp = client.get(ACCOUNT_CODES, **auth)
    assert resp.status_code == 400
    assert resp.json() == {"error": "entity_id is required"}


@pytest.mark.django_db
@pytest.mark.no_currency
def test_the_opening_balance_needs_a_currency(client, auth, entity):
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(), "cash_addition": 10},
    )
    assert resp.status_code == 400
    assert "currency" in resp.json()["error"].lower()
    assert _settings(entity) is None


@pytest.mark.django_db
def test_an_amount_finer_than_the_currency_is_400(client, auth, entity):
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(), "opening_balance": "10.005"},
    )
    assert resp.status_code == 400
