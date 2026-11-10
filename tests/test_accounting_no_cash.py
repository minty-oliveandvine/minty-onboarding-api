"""A company that banks all its sales (4.2's "We bank all sales", ``uses_sales_cash = false``;
owner's brief, 2026-11-10): its Cash sales method is off, and daily closing doesn't ask it for a
cash-sale account. Answering yes (or "Not sure", sent as yes) turns Cash on again.
"""

import pytest
from minty_db.models import EntitySaleSetting, SaleInfo

BASE = "/api/onboarding/accounting"


def post(client, auth, path, payload):
    return client.post(BASE + path, payload, content_type="application/json", **auth)


def _answer(client, auth, entity, cash):
    resp = post(
        client,
        auth,
        "/business",
        {"entity_id": str(entity.id), "business_type": "retail", "uses_sales_cash": cash},
    )
    assert resp.status_code == 200, resp.content
    return resp


def _cash_on(entity):
    link = EntitySaleSetting.objects.filter(
        entity_id=entity.id, sale__value_name="cash_sales"
    ).first()
    return None if link is None else link.is_active


@pytest.fixture
def minty(client, auth, entity):
    assert post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code == 200
    # A new company is linked to Cash when it is made (entity_create's DEFAULT_SALES_METHODS).
    cash = SaleInfo.objects.filter(value_name="cash_sales").first() or SaleInfo.objects.create(
        sale_name="Cash", value_name="cash_sales", type="other", display_order=0
    )
    EntitySaleSetting.objects.create(entity_id=entity.id, sale=cash, is_active=True, display_order=0)
    return entity


@pytest.mark.django_db
def test_banking_all_sales_turns_cash_off_and_yes_turns_it_back_on(client, auth, minty):
    _answer(client, auth, minty, False)
    assert _cash_on(minty) is False
    _answer(client, auth, minty, True)
    assert _cash_on(minty) is True


@pytest.mark.django_db
def test_yes_links_cash_when_the_company_has_no_link_yet(client, auth, entity):
    post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"})
    assert _cash_on(entity) is None
    _answer(client, auth, entity, True)
    assert _cash_on(entity) is True


@pytest.mark.django_db
def test_daily_closing_asks_a_company_banking_all_sales_for_four_accounts(client, auth, minty):
    _answer(client, auth, minty, False)
    body = client.get(BASE, {"entity_id": str(minty.id)}, **auth).json()
    assert set(body["slots"]) == {"petty_cash", "bank", "discrepancy", "director"}

    _answer(client, auth, minty, True)
    body = client.get(BASE, {"entity_id": str(minty.id)}, **auth).json()
    assert "cash_sale" in body["slots"]
