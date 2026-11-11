"""The steps after Accounting Setup follow it (owner, 2026-11-11: "those settings steps should be
updated according to the accounting setup").

- Payment Settings (step 8) lists step 4's expense accounts: a Minty company's own (4.4), a Xero
  company's ticked in X3. A save writes Payment Request's ``account_module`` rows AND the old
  ``entity_bill_account_xero`` rows minty-payment-request-api still reads.
- Choosing petty cash (4.5 / X4) also fills the discrepancy bank, as the old step 6 did.
- Finalize asks for daily closing's accounts as step 4 lists them (no cash-sale account or contact
  for a company banking all its sales) and points there.
- A company banking all its sales keeps no cash-sale contact, and step 5 can't switch Cash back on.
- Resume: with a mode chosen, Petty Cash's step is done once its opening balance is saved.
"""

import datetime

import pytest
from minty_db.models import (
    AccountInfo,
    AccountModule,
    Entity,
    EntityBillAccountXero,
    EntityPettycashSettings,
    EntitySaleSetting,
    SaleInfo,
    XeroContactSync,
)

from core.exceptions import OnboardingValidationError
from onboarding.services import accounting, sales_methods, state, xero_lists
from tests.conftest import requires_postgres

BASE = "/api/onboarding/accounting"
BILLS = "/api/onboarding/bill-codes"


def post(client, auth, path, payload):
    return client.post(path, payload, content_type="application/json", **auth)


def _bills(client, auth, entity):
    return client.get(BILLS, {"entity_id": str(entity.id)}, **auth)


def _save_bills(client, auth, entity, codes, default=None):
    body = {"entity_id": str(entity.id), "selected_codes": codes}
    if default is not None:
        body["default_code"] = default
    return post(client, auth, BILLS, body)


@pytest.fixture
def minty(client, auth, entity):
    assert post(client, auth, BASE + "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code == 200
    Entity.objects.filter(id=entity.id).update(business_type="retail", uses_sales_cash=False)
    entity.refresh_from_db()
    return entity


def _own(entity, code, name, account_type="expense", *, modules=(True, True), user=None):
    """A Minty account as step 4 makes it (``_create`` writes the old rows and the modules)."""
    return accounting._create(
        None, entity, code, name, account_type, None, source="template", modules=modules
    )


def _xero(entity, code, name, account_type="expense", xero_type="EXPENSE"):
    return AccountInfo.objects.create(
        entity_id=entity.id, name=name, code=code, xero_code=code, account_type=account_type,
        type=xero_type, status="ACTIVE", source="xero", xero_account_id=f"x-{code}",
    )


@pytest.fixture
def xero_company(entity, monkeypatch):
    Entity.objects.filter(id=entity.id).update(setup_mode="xero", xero_org_id="org-1", uses_sales_cash=False)
    entity.refresh_from_db()
    rows = {
        "purchases": _xero(entity, "300", "Purchases", "direct_costs", "DIRECTCOSTS"),
        "rent": _xero(entity, "469", "Rent"),
        "fees": _xero(entity, "404", "Bank Fees"),
    }
    return entity, rows


def _pr_module(account):
    return AccountModule.objects.filter(account_id=account.id, module_code="PAYMENT_REQUEST").first()


# ---- step 8: Payment Settings from step 4 -------------------------------------------------------


@pytest.mark.django_db
def test_a_minty_companys_payment_list_is_its_expense_accounts_ticked_from_the_template(client, auth, minty):
    _own(minty, "310", "Stock purchases", "direct_costs", modules=(True, True))
    _own(minty, "420", "Rent", modules=(False, True))
    _own(minty, "6900", "Cash Over / Short", modules=(False, False))
    _own(minty, "200", "Product sales", "sales")  # a sales account is not a bill code

    body = _bills(client, auth, minty).json()
    assert body["connected"] is False
    assert [c["code"] for c in body["bill_codes"]] == ["310", "420", "6900"]
    assert body["selected_codes"] == ["310", "420"]
    assert body["default_all"] is False


@pytest.mark.django_db
def test_a_minty_save_writes_payment_requests_module_rows_and_the_old_rows(client, auth, minty):
    stock = _own(minty, "310", "Stock purchases", "direct_costs")
    rent = _own(minty, "420", "Rent", modules=(True, False))

    resp = _save_bills(client, auth, minty, ["420"], default="420")
    assert resp.status_code == 200, resp.content
    assert resp.json() == {"ok": True, "selected_codes": ["420"], "default_code": "420"}
    assert (_pr_module(rent).is_active, _pr_module(rent).is_default) == (True, True)
    assert (_pr_module(stock).is_active, _pr_module(stock).is_default) == (False, False)
    old = {r.account_code: (r.is_active, r.is_default) for r in EntityBillAccountXero.objects.filter(entity_id=minty.id)}
    assert old == {"310": (False, False), "420": (True, True)}

    # The default moves: one default per module, in both tables.
    assert _save_bills(client, auth, minty, ["310", "420"], default="310").status_code == 200
    assert _pr_module(stock).is_default and not _pr_module(rent).is_default
    body = _bills(client, auth, minty).json()
    assert (body["selected_codes"], body["default_code"]) == (["310", "420"], "310")


@pytest.mark.django_db
def test_a_minty_company_with_no_expense_account_is_sent_back_to_step_4(client, auth, minty):
    read = _bills(client, auth, minty)
    assert read.status_code == 200 and read.json()["bill_codes"] == []
    saved = _save_bills(client, auth, minty, [])
    assert saved.status_code == 409
    assert saved.json()["error"] == "Add an expense account in Accounting Setup first."


@pytest.mark.django_db
def test_the_refusals_keep_their_sentences(client, auth, minty):
    _own(minty, "420", "Rent")
    assert _save_bills(client, auth, minty, []).json() == {"error": "Pick at least one account code."}
    assert _save_bills(client, auth, minty, ["999"]).json() == {"error": "Pick at least one account code."}
    assert _save_bills(client, auth, minty, ["420"], default="999").json() == {
        "error": "The default account code must be one of the ticked codes."}


@pytest.mark.django_db
def test_a_xero_companys_payment_list_is_its_x3_ticks(client, auth, xero_company):
    entity, rows = xero_company
    resp = post(client, auth, BASE + "/xero-accounts",
                {"entity_id": str(entity.id), "kind": "expense",
                 "account_ids": [str(rows["rent"].id), str(rows["purchases"].id)]})
    assert resp.status_code == 200, resp.content

    body = _bills(client, auth, entity).json()
    assert body["connected"] is True
    assert [c["code"] for c in body["bill_codes"]] == ["300", "469"]  # Bank Fees wasn't ticked in X3
    assert body["selected_codes"] == [] and body["default_all"] is True  # nothing chosen yet


@pytest.mark.django_db
def test_a_xero_save_makes_module_rows_and_bill_rows_and_turns_off_the_rest(client, auth, xero_company):
    entity, rows = xero_company
    post(client, auth, BASE + "/xero-accounts",
         {"entity_id": str(entity.id), "kind": "expense", "account_ids": [str(rows["rent"].id)]})
    # The sync's bill row for an account not ticked in X3 (Payment Request would offer it).
    EntityBillAccountXero.objects.create(entity_id=entity.id, account_code="404", account_name="Bank Fees",
                                         account_type="EXPENSE", xero_account_id="x-404", is_active=True)

    resp = _save_bills(client, auth, entity, ["469"], default="469")
    assert resp.status_code == 200, resp.content
    assert (_pr_module(rows["rent"]).is_active, _pr_module(rows["rent"]).is_default) == (True, True)
    old = {r.account_code: (r.xero_account_id, r.is_active, r.is_default)
           for r in EntityBillAccountXero.objects.filter(entity_id=entity.id)}
    assert old == {"469": ("x-469", True, True), "404": ("x-404", False, False)}


@pytest.mark.django_db
def test_a_xero_company_with_nothing_ticked_is_sent_back_to_step_4(client, auth, xero_company):
    entity, _rows = xero_company
    assert _bills(client, auth, entity).json()["bill_codes"] == []
    assert _save_bills(client, auth, entity, ["469"]).status_code == 409


# ---- step 4's petty cash fills the discrepancy bank ---------------------------------------------


@pytest.mark.django_db
def test_choosing_petty_cash_also_fills_the_discrepancy_bank(client, auth, minty):
    float_ = _own(minty, "1000", "Petty Cash", "bank")
    resp = post(client, auth, BASE + "/slots",
                {"entity_id": str(minty.id), "role": "petty_cash", "account_id": str(float_.id)})
    assert resp.status_code == 200, resp.content
    settings = EntityPettycashSettings.objects.get(entity_id=minty.id)
    assert settings.pettycash_account_id == settings.discrepancy_bank_account_id == float_.id

    post(client, auth, BASE + "/slots", {"entity_id": str(minty.id), "role": "petty_cash", "account_id": None})
    settings.refresh_from_db()
    assert settings.pettycash_account_id is None and settings.discrepancy_bank_account_id is None


# ---- step 7: no cash-sale contact without cash sales -------------------------------------------


@pytest.mark.django_db
def test_a_company_banking_all_sales_keeps_no_cash_sale_contact(client, auth, xero_company, monkeypatch):
    entity, _rows = xero_company
    monkeypatch.setattr(xero_lists, "access_token_for", lambda entity_id: "tok")
    for i, name in enumerate(("Director", "Cash buyer", "Over and short")):
        XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id=f"c{i}", name=name)
    old = XeroContactSync.objects.get(xero_contact_id="c1")
    EntityPettycashSettings.objects.create(entity_id=entity.id, cash_sale_contact_id=old.id)

    resp = post(client, auth, "/api/onboarding/contacts",
                {"entity_id": str(entity.id),
                 "contacts": {"director": "c0", "cash_sale": "c1", "discrepancy": "c2"}})
    assert resp.status_code == 200, resp.content
    settings = EntityPettycashSettings.objects.get(entity_id=entity.id)
    assert settings.cash_sale_contact_id is None
    assert settings.director_contact_id and settings.discrepancy_contact_id


# ---- step 5: Cash follows step 4's answer ------------------------------------------------------


@pytest.mark.django_db
def test_a_method_named_cash_is_refused_in_step_5(user, entity):
    # A company with no mode still edits step 5 (one with a mode can't at all: test_sales_categories).
    SaleInfo.objects.create(sale_name="Cash", value_name="cash_sales", type="other", display_order=0)
    with pytest.raises(OnboardingValidationError) as refused:
        sales_methods.replace(user.id, str(entity.id), ["Octopus", "cash"], [])
    assert str(refused.value) == "Cash is set by your answer in Accounting Setup."
    assert not EntitySaleSetting.objects.filter(entity_id=entity.id).exists()


# ---- resume ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_minty_company_resumes_at_petty_cash_until_its_opening_balance_is_saved(minty):
    assert state.derive_current_step(minty, ["PETTY_CASH"], {"connected": False}, False) == 5
    assert state.derive_current_step(minty, ["PETTY_CASH"], {"connected": False}, True) == 3


@requires_postgres
@pytest.mark.django_db
def test_resume_reads_the_saved_opening_balance(user, minty, enable_module):
    enable_module(minty, "PETTY_CASH")
    assert state.get_onboarding_state(user.id, minty)["current_step"] == 5
    EntityPettycashSettings.objects.create(entity_id=minty.id, start_date=datetime.date(2026, 11, 1))
    assert state.get_onboarding_state(user.id, minty)["current_step"] == 3


# ---- finalize follows step 4 (the gaps read entity_setup_summary: Postgres) ----------------------


def _slots(entity, roles):
    accounts = {
        r: AccountInfo.objects.filter(entity_id=entity.id, xero_account_id=f"x-9{i}").first()
        or _xero(entity, f"9{i}", r.title(), "bank", "BANK")
        for i, r in enumerate(accounting.SLOTS) if r in roles
    }
    EntityPettycashSettings.objects.update_or_create(
        entity_id=entity.id, defaults={accounting.SLOTS[r][0]: a.id for r, a in accounts.items()})


def _contacts(entity, roles):
    ids = {}
    for role in roles:
        ids[f"{role}_contact_id"] = XeroContactSync.objects.create(
            entity_id=entity.id, xero_contact_id=f"c-{role}", name=role).id
    EntityPettycashSettings.objects.update_or_create(entity_id=entity.id, defaults=ids)


@requires_postgres
@pytest.mark.django_db
def test_a_xero_company_banking_all_sales_needs_four_accounts_and_two_contacts(xero_company, enable_module):
    from onboarding.api_billing import setup_gaps

    entity, _rows = xero_company
    enable_module(entity, "PETTY_CASH")
    _slots(entity, ["petty_cash", "bank", "discrepancy"])
    gaps = setup_gaps(entity)
    assert {"step": 6, "message": "Choose the accounts daily closing needs (3 of 4 set)."} in gaps
    assert any(g["step"] == 7 for g in gaps)

    _slots(entity, ["petty_cash", "bank", "discrepancy", "director"])
    _contacts(entity, ["director", "discrepancy"])
    assert setup_gaps(entity) == []


@requires_postgres
@pytest.mark.django_db
def test_a_xero_company_with_cash_sales_also_needs_the_cash_sale_account_and_contact(xero_company, enable_module):
    from onboarding.api_billing import setup_gaps

    entity, _rows = xero_company
    Entity.objects.filter(id=entity.id).update(uses_sales_cash=True)
    entity.refresh_from_db()
    enable_module(entity, "PETTY_CASH")
    _slots(entity, ["petty_cash", "bank", "discrepancy", "director"])
    _contacts(entity, ["director", "discrepancy"])
    gaps = setup_gaps(entity)
    assert {"step": 6, "message": "Choose the accounts daily closing needs (4 of 5 set)."} in gaps
    assert {"step": 7, "message": "Choose the petty cash contacts (2 of 3 set)."} in gaps
