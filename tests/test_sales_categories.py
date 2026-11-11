"""Petty Cash's sales methods are Accounting Setup's sales categories (owner, 2026-11-11).

Step 5 shows step 4's sales categories (4.3, or X2's ticks on Xero) as one read-only list. Each is a
sales method linked to its account (``entity_sale_setting.account_id``, minty-db v0.6); every step-4
change re-links them (``sales_methods.sync_from_accounts``):
- a category -> the catalogue row of its name, typed delivery when its name says so, else
  electronic (Petty Cash still groups by type until its own one-list release);
- the cash-sale account is the Cash method's account, never a second method; Cash stays on or off
  by the cash answer;
- other income isn't taken at the till: no method;
- a renamed, removed, retyped or unticked category's link is switched off and unlinked (kept for
  report history), as is any older method with no account.
"""

import pytest
from minty_db.models import (
    AccountInfo,
    Entity,
    EntitySaleSetting,
    SaleInfo,
)

BASE = "/api/onboarding/accounting"
SALES = "/api/onboarding/sales-methods"


def post(client, auth, path, payload):
    return client.post(path, payload, content_type="application/json", **auth)


def _add(client, auth, entity, code, name, type_="sales", **extra):
    resp = post(client, auth, BASE + "/accounts",
                {"entity_id": str(entity.id), "code": code, "name": name, "type": type_, **extra})
    assert resp.status_code == 201, resp.content
    return resp.json()["account"]


def _links(entity):
    """{method name: (is_active, account name or None, type)} for the company's links."""
    out = {}
    for link in EntitySaleSetting.objects.filter(entity_id=entity.id).select_related("sale"):
        account = AccountInfo.objects.filter(id=link.account_id).first() if link.account_id else None
        out[link.sale.sale_name] = (link.is_active, account.name if account else None, link.sale.type)
    return out


def _categories(client, auth, entity):
    resp = client.get(SALES, {"entity_id": str(entity.id)}, **auth)
    assert resp.status_code == 200, resp.content
    return [(c["code"], c["name"], c["method"]) for c in resp.json()["categories"]]


@pytest.fixture
def minty(client, auth, entity):
    SaleInfo.objects.get_or_create(sale_name="Cash", defaults={"value_name": "cash_sales", "type": "other",
                                                                "display_order": 0})
    assert post(client, auth, BASE + "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code == 200
    resp = post(client, auth, BASE + "/business",
                {"entity_id": str(entity.id), "business_type": "retail", "uses_sales_cash": True})
    assert resp.status_code == 200, resp.content
    entity.refresh_from_db()
    return entity


@pytest.mark.django_db
def test_the_sales_categories_are_the_sales_methods(client, auth, minty):
    _add(client, auth, minty, "201", "Online Sales")
    _add(client, auth, minty, "202", "Delivery Sales")
    _add(client, auth, minty, "260", "Interest Income", "other_income")
    _add(client, auth, minty, "203", "Cash Sales", role="cash_sale")

    links = _links(minty)
    assert links["Online Sales"] == (True, "Online Sales", "electronic")
    assert links["Delivery Sales"] == (True, "Delivery Sales", "delivery")
    assert links["Cash"] == (True, "Cash Sales", "other")  # the cash-sale account is Cash's
    assert "Cash Sales" not in links and "Interest Income" not in links
    assert _categories(client, auth, minty) == [
        ("201", "Online Sales", "Online Sales"),
        ("202", "Delivery Sales", "Delivery Sales"),
        ("203", "Cash Sales", "Cash"),
    ]


@pytest.mark.django_db
def test_a_renamed_category_moves_its_method_and_keeps_the_old_one_off(client, auth, minty):
    online = _add(client, auth, minty, "201", "Online Sales")
    resp = client.patch(f"{BASE}/accounts/{online['id']}",
                        {"entity_id": str(minty.id), "code": "201", "name": "Web Sales", "type": "sales"},
                        content_type="application/json", **auth)
    assert resp.status_code == 200, resp.content
    links = _links(minty)
    assert links["Online Sales"] == (False, None, "electronic")
    assert links["Web Sales"] == (True, "Web Sales", "electronic")


@pytest.mark.django_db
def test_a_removed_or_retyped_category_switches_its_method_off(client, auth, minty):
    online = _add(client, auth, minty, "201", "Online Sales")
    tips = _add(client, auth, minty, "205", "Tips")
    assert client.delete(f"{BASE}/accounts/{online['id']}?entity_id={minty.id}", **auth).status_code == 200
    resp = client.patch(f"{BASE}/accounts/{tips['id']}",
                        {"entity_id": str(minty.id), "code": "405", "name": "Tips", "type": "expense"},
                        content_type="application/json", **auth)
    assert resp.status_code == 200, resp.content
    links = _links(minty)
    assert links["Online Sales"][:2] == (False, None)
    assert links["Tips"][:2] == (False, None)
    assert _categories(client, auth, minty) == []


@pytest.mark.django_db
def test_an_older_method_with_no_account_is_switched_off(client, auth, minty):
    visa = SaleInfo.objects.create(sale_name="Visa", value_name="visa_sales", type="electronic")
    EntitySaleSetting.objects.create(entity_id=minty.id, sale=visa, is_active=True, display_order=1)
    _add(client, auth, minty, "201", "Online Sales")
    assert _links(minty)["Visa"] == (False, None, "electronic")


@pytest.mark.django_db
def test_banking_all_sales_keeps_cash_off(client, auth, minty):
    post(client, auth, BASE + "/business",
         {"entity_id": str(minty.id), "business_type": "retail", "uses_sales_cash": False})
    _add(client, auth, minty, "201", "Online Sales")
    links = _links(minty)
    assert links["Cash"][0] is False
    assert [c[2] for c in _categories(client, auth, minty)] == ["Online Sales"]


@pytest.mark.django_db
def test_a_xero_companys_methods_are_its_x2_ticks(client, auth, entity):
    Entity.objects.filter(id=entity.id).update(setup_mode="xero", xero_org_id="org-1", uses_sales_cash=True)
    entity.refresh_from_db()
    shop, online = (
        AccountInfo.objects.create(entity_id=entity.id, name=name, code=code, xero_code=code,
                                   account_type="sales", type="REVENUE", status="ACTIVE", source="xero",
                                   xero_account_id=f"x-{code}")
        for code, name in (("200", "Shop Sales"), ("210", "Online Sales"))
    )

    def tick(*accounts):
        resp = post(client, auth, BASE + "/xero-accounts",
                    {"entity_id": str(entity.id), "kind": "sales", "account_ids": [str(a.id) for a in accounts]})
        assert resp.status_code == 200, resp.content

    tick(shop, online)
    assert [c[2] for c in _categories(client, auth, entity)] == ["Shop Sales", "Online Sales"]
    tick(online)
    assert _links(entity)["Shop Sales"][:2] == (False, None)
    assert [c[2] for c in _categories(client, auth, entity)] == ["Online Sales"]


@pytest.mark.django_db
def test_step_5_cannot_edit_the_methods_of_a_company_with_a_mode(client, auth, minty):
    resp = post(client, auth, SALES, {"entity_id": str(minty.id), "electronic": ["Visa"], "delivery": []})
    assert resp.status_code == 409
    assert resp.json()["error"] == "Your sales categories come from Accounting Setup. Change them there."


@pytest.mark.django_db
def test_a_company_with_no_mode_still_edits_step_5(client, auth, entity):
    resp = post(client, auth, SALES, {"entity_id": str(entity.id), "electronic": ["Visa"], "delivery": []})
    assert resp.status_code == 200, resp.content


@pytest.mark.django_db
def test_resume_carries_the_categories(client, auth, minty):
    from onboarding.services import state

    _add(client, auth, minty, "201", "Online Sales")
    assert state._sales_methods_state(str(minty.id))["categories"][0]["name"] == "Online Sales"
