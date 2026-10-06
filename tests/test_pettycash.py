"""Group D -- petty-cash configuration.

D1 (sales methods, opening balance) is tested as a port. D2 is tested as a PROXY: the
assertions are about what gets forwarded and what comes back, not about Xero, because Xero is
Flask's business.

The opening-balance tests carry the weight. Its two different lookup keys -- entity alone
before onboarding ends, exact date after -- are the kind of thing that looks arbitrary and is
not, and either half getting lost is a silent data bug rather than an error.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from core import minty_client
from core.exceptions import UpstreamError
from shared_models.models import Entity, EntitySaleSetting, Report, SaleInfo, UserEntity
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
@pytest.mark.django_db
def test_opening_balance_seeds_a_draft(client, auth, entity):
    today = date.today().isoformat()
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": today, "cash_addition": 1500.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["opening_balance"] == 1500.0
    assert body["cash_addition"] == 0.0
    assert body["adjusted_opening_balance"] == 1500.0


@pytest.mark.django_db
def test_the_amount_is_an_opening_balance_not_an_addition(client, auth, entity):
    """The distinction the whole service exists to get right.

    Recording it as cash_addition would make the starting drawer look like money someone put
    in on day one. The adjusted opening is the same either way, so the arithmetic does not
    change -- only what the report says happened.
    """
    post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(),
         "cash_addition": 900.0},
    )
    draft = Report.objects.get(entity_id=entity.id, status="draft")
    assert draft.opening_balance == 900.0
    assert draft.cash_addition == 0.0
    assert draft.adjusted_opening_balance == 900.0
    assert draft.closing_balance == 900.0


@pytest.mark.django_db
def test_the_draft_starts_at_the_opening_section(client, auth, entity):
    """Seeded, but not pre-marked complete: the user still confirms it in their first report."""
    post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(), "cash_addition": 1},
    )
    draft = Report.objects.get(entity_id=entity.id, status="draft")
    assert draft.current_section == "opening"
    assert draft.completed_sections == []


@pytest.mark.django_db
def test_next_transaction_date_is_the_following_day(client, auth, entity):
    today = date.today()
    post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": today.isoformat(), "cash_addition": 1},
    )
    draft = Report.objects.get(entity_id=entity.id, status="draft")
    assert draft.next_transaction_date == today + timedelta(days=1)


@pytest.mark.django_db
def test_changing_the_date_moves_the_same_draft(client, auth, entity):
    """THE FIRST OF THE TWO KEYS: entity alone, while onboarding is still running.

    Keying on (entity, date) here would leave a stale draft behind every time the user
    edited the opening date, and `/state` would then read the wrong one.
    """
    first = date.today() - timedelta(days=5)
    second = date.today() - timedelta(days=2)

    a = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": first.isoformat(), "cash_addition": 100},
    ).json()
    b = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": second.isoformat(), "cash_addition": 250},
    ).json()

    assert b["created"] is False
    assert b["draft_id"] == a["draft_id"]
    assert Report.objects.filter(entity_id=entity.id).count() == 1
    draft = Report.objects.get(entity_id=entity.id)
    assert draft.transaction_date == second
    assert draft.opening_balance == 250.0


@pytest.mark.django_db
def test_after_a_posted_report_the_key_becomes_the_exact_date(client, auth, entity):
    """THE SECOND KEY. Onboarding is over, so this must not reach into another day's draft.

    There is a posted report and an in-progress draft for an unrelated day. Seeding an
    opening for a third date must create its own row and leave that draft alone.
    """
    posted_day = date.today() - timedelta(days=10)
    other_day = date.today() - timedelta(days=3)
    target_day = date.today() - timedelta(days=1)

    Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, status="submitted",
        transaction_date=posted_day, opening_balance=0.0, cash_addition=0.0,
    )
    untouched = Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, status="draft",
        transaction_date=other_day, opening_balance=777.0, cash_addition=0.0,
    )

    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": target_day.isoformat(), "cash_addition": 50},
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is True

    untouched.refresh_from_db()
    assert untouched.opening_balance == 777.0
    assert untouched.transaction_date == other_day


@pytest.mark.django_db
def test_a_posted_report_on_the_same_date_is_409(client, auth, entity):
    day = date.today() - timedelta(days=1)
    Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, status="submitted",
        transaction_date=day, opening_balance=0.0, cash_addition=0.0,
    )
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": day.isoformat(), "cash_addition": 10},
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["error"]


@pytest.mark.django_db
def test_a_published_report_counts_as_posted(client, auth, entity):
    """Any status but draft is a finished report (``status`` is NOT NULL since C4)."""
    day = date.today() - timedelta(days=1)
    Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, status="published",
        transaction_date=day, opening_balance=0.0, cash_addition=0.0,
    )
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": day.isoformat(), "cash_addition": 10},
    )
    assert resp.status_code == 409


@pytest.mark.django_db
def test_our_own_draft_does_not_block_a_resave(client, auth, entity):
    """THE BUG THE POSTED-ONLY FILTER FIXES.

    An unfiltered existence check finds the draft this function wrote on the previous call
    and refuses to update it. Flask's comment records that this is what "broke the all-set
    page after the r0 backfill ran".
    """
    day = date.today().isoformat()
    body = {"entity_id": entity.id, "opening_date": day, "cash_addition": 100}
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


@pytest.mark.django_db
def test_an_old_past_date_is_allowed(client, auth, entity):
    """Onboarding deliberately has NO seven-day window, unlike the regular first report."""
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
def test_opening_balance_is_accepted_as_the_legacy_amount_key(client, auth, entity):
    resp = post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "transaction_date": date.today().isoformat(),
         "opening_balance": 42},
    )
    assert resp.status_code == 200
    assert resp.json()["opening_balance"] == 42.0


@pytest.mark.django_db
def test_created_by_records_the_person(client, auth, entity, user):
    """``report.created_by`` is the person's id (was the username in ``uploaded_by``)."""
    post_json(
        client, auth, OPENING,
        {"entity_id": entity.id, "opening_date": date.today().isoformat(),
         "cash_addition": 1},
    )
    assert str(Report.objects.get(entity_id=entity.id).created_by) == str(user.id)


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
