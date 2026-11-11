"""Accounting Setup without its sales screen (owner, 2026-11-11): answering the business type (4.2)
adds that set's sales accounts from the accounting team's sheet, as it adds the shared daily-closing
rows - so daily closing's Cash Sales account is filled with no screen to choose it. Changing the
answer swaps the set (unused template sales rows out, the new set in); "We bank all sales" has no
Cash Sales account. A Xero company gets none: its accounts come from Xero.
"""

import pytest
from minty_db.models import AccountInfo, AccountTemplate, EntityPettycashSettings

BASE = "/api/onboarding/accounting"


def post(client, auth, path, payload):
    return client.post(BASE + path, payload, content_type="application/json", **auth)


@pytest.fixture
def sheet(db, countries):
    rows = [
        (None, None, "1000", "Petty Cash", "bank", "petty_cash"),
        (None, None, "1001", "Bank Account", "bank", "bank"),
        (None, None, "2100", "Amount Due to Director", "current_liability", "director"),
        (None, None, "6900", "Cash Over / Short", "expense", "discrepancy"),
        ("retail", True, "4000", "Cash Sales", "sales", "cash_sale"),
        ("retail", True, "4001", "Card Sales", "sales", None),
        ("retail", False, "4001", "Card Sales", "sales", None),
        ("retail", True, "5000", "Inventory Purchases", "direct_costs", None),
        ("fnb", True, "4100", "Cash Sales", "sales", "cash_sale"),
        ("fnb", True, "4102", "Delivery Sales", "sales", None),
    ]
    for business, cash, code, name, kind, role in rows:
        AccountTemplate.objects.create(business_type=business, uses_sales_cash=cash, code=code, name=name,
                                       type=kind, pettycash_role=role)


def _answer(client, auth, entity, business, cash):
    resp = post(client, auth, "/business",
                {"entity_id": str(entity.id), "business_type": business, "uses_sales_cash": cash})
    assert resp.status_code == 200, resp.content


def _sales(entity):
    return sorted(AccountInfo.objects.filter(entity_id=entity.id, account_type="sales")
                  .exclude(record_status="archived").values_list("code", "name"))


@pytest.fixture
def minty(client, auth, entity, sheet):
    assert post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code == 200
    entity.refresh_from_db()
    return entity


@pytest.mark.django_db
def test_the_business_type_adds_its_sales_accounts_and_fills_cash_sales(client, auth, minty):
    _answer(client, auth, minty, "retail", True)
    assert _sales(minty) == [("4000", "Cash Sales"), ("4001", "Card Sales")]
    assert not AccountInfo.objects.filter(entity_id=minty.id, code="5000").exists(), "expenses stay 4.4's"
    settings = EntityPettycashSettings.objects.get(entity_id=minty.id)
    assert AccountInfo.objects.get(id=settings.cash_sale_account_id).name == "Cash Sales"
    assert all(getattr(settings, c) for c in ("pettycash_account_id", "bank_account_id", "director_account_id",
                                              "discrepancy_account_id"))


@pytest.mark.django_db
def test_another_answer_swaps_the_set(client, auth, minty):
    _answer(client, auth, minty, "retail", True)
    _answer(client, auth, minty, "fnb", True)
    assert _sales(minty) == [("4100", "Cash Sales"), ("4102", "Delivery Sales")]
    settings = EntityPettycashSettings.objects.get(entity_id=minty.id)
    assert AccountInfo.objects.get(id=settings.cash_sale_account_id).code == "4100"


@pytest.mark.django_db
def test_banking_all_sales_has_no_cash_sales_account(client, auth, minty):
    _answer(client, auth, minty, "retail", True)
    _answer(client, auth, minty, "retail", False)
    assert _sales(minty) == [("4001", "Card Sales")]
    assert EntityPettycashSettings.objects.get(entity_id=minty.id).cash_sale_account_id is None


@pytest.mark.django_db
def test_a_xero_company_gets_no_sales_accounts(client, auth, entity, sheet):
    post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "xero"})
    entity.refresh_from_db()
    _answer(client, auth, entity, "retail", True)
    assert _sales(entity) == []
