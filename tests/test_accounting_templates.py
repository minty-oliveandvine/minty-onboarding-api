"""Auto Fill follows the accounting team's sheet (2026-11-10): each template row's Petty Cash / Bill
columns decide which modules the new account shows in, and a row's daily-closing role (e.g. Cash
Sales) fills that account when it is still empty. An account the person adds stays in both."""

import pytest
from minty_db.models import (
    AccountInfo,
    AccountModule,
    AccountTemplate,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
)

BASE = "/api/onboarding/accounting"


def post(client, auth, path, payload):
    return client.post(BASE + path, payload, content_type="application/json", **auth)


@pytest.fixture
def minty(client, auth, entity, countries):
    assert post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code == 200
    rows = {
        "cash": AccountTemplate.objects.create(business_type="retail", uses_sales_cash=True, code="4000",
                                               name="Cash Sales", type="sales", pettycash_role="cash_sale"),
        "card": AccountTemplate.objects.create(business_type="retail", uses_sales_cash=True, code="4001",
                                               name="Card Sales", type="sales"),
        "stock": AccountTemplate.objects.create(business_type="retail", uses_sales_cash=True, code="5000",
                                                name="Inventory Purchases", type="direct_costs",
                                                pettycash_default=False, payment_request_default=True),
        "clean": AccountTemplate.objects.create(business_type="retail", uses_sales_cash=True, code="6000",
                                                name="Cleaning", type="expense",
                                                pettycash_default=True, payment_request_default=False),
        "wages": AccountTemplate.objects.create(business_type="retail", uses_sales_cash=True, code="6005",
                                                name="Wages and Salaries", type="expense",
                                                pettycash_default=True, payment_request_default=True),
    }
    resp = post(client, auth, "/business",
                {"entity_id": str(entity.id), "business_type": "retail", "uses_sales_cash": True})
    assert resp.status_code == 200, resp.content
    entity.refresh_from_db()
    return entity, rows


def _fill(client, auth, entity, kind, templates):
    resp = post(client, auth, "/autofill",
                {"entity_id": str(entity.id), "kind": kind, "template_ids": [str(t.id) for t in templates]})
    assert resp.status_code == 200, resp.content


def _modules(entity, code):
    account = AccountInfo.objects.get(entity_id=entity.id, code=code)
    rows = {m.module_code: m.is_active for m in AccountModule.objects.filter(account_id=account.id)}
    petty = EntityAccountXero.objects.filter(account_id=account.id, is_active=True).exists()
    bill = EntityBillAccountXero.objects.filter(entity_id=entity.id, account_code=code, is_active=True).exists()
    return rows.get("PETTY_CASH", False), rows.get("PAYMENT_REQUEST", False), petty, bill


@pytest.mark.django_db
def test_each_account_shows_in_the_modules_its_row_ticks(client, auth, minty):
    entity, rows = minty
    _fill(client, auth, entity, "expense", [rows["stock"], rows["clean"], rows["wages"]])
    # (Petty Cash module, Payment Request module, Petty Cash's old tick, Payment Request's old code)
    assert _modules(entity, "5000") == (False, True, False, True)
    assert _modules(entity, "6000") == (True, False, True, False)
    assert _modules(entity, "6005") == (True, True, True, True)


@pytest.mark.django_db
def test_a_row_with_a_daily_closing_role_fills_it_when_empty(client, auth, minty):
    entity, rows = minty
    _fill(client, auth, entity, "sales", [rows["cash"], rows["card"]])
    cash = AccountInfo.objects.get(entity_id=entity.id, code="4000")
    assert EntityPettycashSettings.objects.get(entity_id=entity.id).cash_sale_account_id == cash.id


@pytest.mark.django_db
def test_a_role_already_chosen_is_left_alone(client, auth, minty):
    entity, rows = minty
    mine = post(client, auth, "/accounts", {"entity_id": str(entity.id), "code": "4100", "name": "Till Sales",
                                            "type": "sales", "role": "cash_sale"})
    assert mine.status_code in (200, 201), mine.content
    _fill(client, auth, entity, "sales", [rows["cash"]])
    chosen = EntityPettycashSettings.objects.get(entity_id=entity.id).cash_sale_account_id
    assert chosen == AccountInfo.objects.get(entity_id=entity.id, code="4100").id


@pytest.mark.django_db
def test_an_account_the_person_adds_is_in_both_modules(client, auth, minty):
    entity, _rows = minty
    resp = post(client, auth, "/accounts",
                {"entity_id": str(entity.id), "code": "6100", "name": "Postage", "type": "expense"})
    assert resp.status_code in (200, 201), resp.content
    assert _modules(entity, "6100") == (True, True, True, True)
