"""The wizard's Xero lists (``onboarding/services/xero_lists.py``), done here since Phase 4 of the
Flask retirement.

Restated from Flask:
- ``tests/test_xero_data_and_onboarding_codes.py`` and ``test_onboarding_round2.py``: the codes'
  rules;
- ``onboarding_account_codes.py``'s and ``onboarding_bill_codes.py``'s own behaviour, since Flask
  had no tests for the contacts or a new contact.

Xero and accounts-api's sync are faked at ``core.xero_client.requests``; the token service at
``access_token_for``.
"""

from __future__ import annotations

import pytest
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    UserEntity,
    XeroContactSync,
)

from core import xero_client
from onboarding.services import xero_lists
from tests.conftest import make_token

ORG = "org-1"
CODES = "/api/onboarding/account-codes"
CONTACTS = "/api/onboarding/contacts"
CREATE = "/api/onboarding/contacts/create"
BILLS = "/api/onboarding/bill-codes"

ACCOUNTS = [
    {"AccountID": "a-bank2", "Code": "091", "Name": "Savings", "Type": "BANK", "BankAccountNumber": "12345678"},
    {"AccountID": "a-bank1", "Code": "090", "Name": "Operating", "Type": "BANK"},
    {"AccountID": "a-sales", "Code": "200", "Name": "Sales", "Type": "REVENUE"},
    {"AccountID": "a-loan", "Code": "880", "Name": "Director Loan", "Type": "CURRLIAB"},
    {"AccountID": "a-gst", "Code": "820", "Name": "GST", "Type": "CURRLIAB", "SystemAccount": "GST"},
    {"AccountID": "a-fees", "Code": "404", "Name": "Bank Fees", "Type": "EXPENSE"},
    {"AccountID": "a-adv", "Code": "400", "Name": "Advertising", "Type": "EXPENSE"},
    {"AccountID": "a-round", "Code": "860", "Name": "Rounding", "Type": "EXPENSE", "SystemAccount": "ROUNDING"},
]


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class Xero:
    def __init__(self):
        self.accounts = list(ACCOUNTS)
        self.contacts = [{"ContactID": "c-2", "Name": "Zeta Supplies"}, {"ContactID": "c-1", "Name": "Alpha"},
                         {"ContactID": "c-1", "Name": "Alpha"}]
        self.down = False
        self.calls: list[tuple] = []
        self.on_sync = None  # what accounts-api's sync stores, when asked

    def get(self, url, headers=None, params=None, timeout=None, **_):
        self.calls.append(("GET", url.rsplit("/", 1)[-1], params))
        assert headers["Xero-Tenant-Id"] == ORG and headers["Authorization"] == "Bearer tok"
        if self.down:
            return _Resp(503, {})
        if url.endswith("/Accounts"):
            return _Resp(200, {"Accounts": self.accounts})
        return _Resp(200, {"Contacts": self.contacts if params["page"] == 1 else []})

    def post(self, url, headers=None, json=None, timeout=None, **_):
        if url.endswith("/xero/sync"):
            self.calls.append(("SYNC",))
            if self.on_sync:
                self.on_sync()
            return _Resp(200, {"accounts": None, "contacts": None})
        self.calls.append(("POST", "Contacts", json))
        if self.down:
            return _Resp(400, {})
        return _Resp(200, {"Contacts": [{"ContactID": "c-new", "Name": json["Contacts"][0]["Name"]}]})


@pytest.fixture
def xero(monkeypatch) -> Xero:
    fake = Xero()
    monkeypatch.setattr(xero_client.requests, "get", fake.get)
    monkeypatch.setattr(xero_client.requests, "post", fake.post)
    monkeypatch.setattr(xero_lists, "access_token_for", lambda entity_id: "tok")
    return fake


@pytest.fixture
def connected(entity):
    Entity.objects.filter(id=entity.id).update(xero_org_id=ORG)
    return entity


def stored(entity, *accounts):
    """``account_info`` rows for the given Xero accounts (as a sync would store them)."""
    by_id = {a["AccountID"]: a for a in ACCOUNTS}
    rows = []
    for account_id in accounts:
        a = by_id[account_id]
        rows.append(AccountInfo.objects.create(entity_id=entity.id, xero_account_id=account_id, xero_code=a["Code"],
                                               name=a["Name"], type=a["Type"], status="ACTIVE"))
    return rows


def post(client, auth, path, body):
    return client.post(path, body, content_type="application/json", **auth)


def as_member(entity, role):
    from minty_db.models import User

    person = User.objects.create(email=f"{role}@test.com", username=f"{role}@test.com", password="x",
                                 first_name="Cass", last_name="Member", approved=True)
    UserEntity.objects.create(user_id=person.id, entity_id=entity.id, role=role, approved=True)
    return {"HTTP_AUTHORIZATION": f"Bearer {make_token(person.id)}"}


# ---- step 5: the read -------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_lists_come_live_from_xero_sorted_and_without_system_accounts(client, auth, connected, xero):
    resp = client.get(CODES, {"entity_id": connected.id}, **auth)

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["connected"] is True
    assert body["bank_accounts"] == [{"id": "a-bank1", "label": "Operating"},
                                     {"id": "a-bank2", "label": "Savings - ****5678"}]
    assert body["cash_sale_accounts"] == [{"id": "a-sales", "label": "200 · Sales"}]
    assert body["director_accounts"] == [{"id": "a-loan", "label": "880 · Director Loan"}]  # GST is a system account
    assert body["discrepancy_accounts"] == [{"id": "a-adv", "label": "400 · Advertising"},
                                            {"id": "a-fees", "label": "404 · Bank Fees"},
                                            {"id": "a-round", "label": "860 · Rounding"}]
    assert body["expense_codes"] == [{"code": "400", "name": "Advertising"}, {"code": "404", "name": "Bank Fees"}]
    assert body["contacts"] == [{"id": "c-2", "label": "Zeta Supplies"}, {"id": "c-1", "label": "Alpha"}]
    assert (body["selected_codes"], body["default_all"]) == ([], True)
    assert body["mapping_defaults"] == dict.fromkeys(("pettycash", "deposit", "director", "cash_sale", "discrepancy"))
    assert ("GET", "Accounts", {"where": 'Status=="ACTIVE"', "order": "Code"}) in xero.calls


@pytest.mark.django_db
def test_the_read_shows_the_saved_choices_and_ticks(client, auth, connected, xero):
    bank, adv, fees = stored(connected, "a-bank1", "a-adv", "a-fees")
    EntityAccountXero.objects.create(account_id=adv.id, name="Advertising", type="EXPENSE", is_active=True)
    EntityAccountXero.objects.create(account_id=fees.id, name="Bank Fees", type="EXPENSE", is_active=False)
    contact = XeroContactSync.objects.create(entity_id=connected.id, xero_contact_id="c-1", name="Alpha")
    EntityPettycashSettings.objects.create(entity_id=connected.id, pettycash_account_id=bank.id,
                                           director_contact_id=contact.id)

    body = client.get(CODES, {"entity_id": connected.id}, **auth).json()

    assert (body["selected_codes"], body["default_all"]) == (["400"], False)
    assert body["mapping_defaults"]["pettycash"] == "a-bank1"
    assert body["contact_defaults"] == {"director": "c-1", "cash_sale": None, "discrepancy": None}


@pytest.mark.django_db
def test_no_xero_yet_is_flasks_409(client, auth, entity, xero, monkeypatch):
    resp = client.get(CODES, {"entity_id": entity.id}, **auth)
    assert (resp.status_code, resp.json()) == (409, {"error": "Connect to Xero first to load account codes.",
                                                      "connected": False})
    Entity.objects.filter(id=entity.id).update(xero_org_id=ORG)
    monkeypatch.setattr(xero_lists, "access_token_for", lambda entity_id: None)  # no working token
    assert client.get(CODES, {"entity_id": entity.id}, **auth).status_code == 409
    assert xero.calls == []


@pytest.mark.django_db
def test_xero_not_answering_is_a_502_with_a_sentence(client, auth, connected, xero):
    xero.down = True
    resp = client.get(CODES, {"entity_id": connected.id}, **auth)
    assert (resp.status_code, resp.json()) == (502, {"error": "Xero didn't send your accounts just now. "
                                                               "Try again in a moment."})


@pytest.mark.django_db
def test_a_base_member_is_refused(client, connected, xero):
    base = as_member(connected, "entity_base")
    assert client.get(CODES, {"entity_id": connected.id}, **base).json() == {"error": "Access denied"}


# ---- step 5: the save -------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_mapping_and_codes_are_saved_after_a_sync(client, auth, connected, xero):
    xero.on_sync = lambda: stored(connected, "a-bank1", "a-bank2", "a-sales", "a-loan", "a-adv", "a-fees")
    body = {"entity_id": connected.id, "expense_codes": ["400"],
            "mapping": {"pettycash": "a-bank1", "deposit": "a-bank2", "director": "a-loan", "cash_sale": "a-sales",
                        "discrepancy": "a-adv"}}

    resp = post(client, auth, CODES, body)

    assert (resp.status_code, resp.json()) == (200, {"ok": True, "expense_codes": ["400"]})
    assert xero.calls == [("SYNC",)]  # once: everything was stored by then
    row = EntityPettycashSettings.objects.get(entity_id=connected.id)
    rows = {a.xero_account_id: a.id for a in AccountInfo.objects.filter(entity_id=connected.id)}
    assert (row.pettycash_account_id, row.bank_account_id, row.director_account_id) == (
        rows["a-bank1"], rows["a-bank2"], rows["a-loan"])
    assert row.discrepancy_bank_account_id == rows["a-bank1"]  # it mirrors the petty cash bank
    ticks = dict(EntityAccountXero.objects.filter(account__entity_id=connected.id)
                 .values_list("account__xero_code", "is_active"))
    assert ticks == {"400": True, "404": False}


@pytest.mark.django_db
def test_an_absent_key_is_kept_and_an_empty_one_cleared(client, auth, connected, xero):
    bank, deposit, loan, adv = stored(connected, "a-bank1", "a-bank2", "a-loan", "a-adv")
    EntityPettycashSettings.objects.create(entity_id=connected.id, pettycash_account_id=bank.id,
                                           bank_account_id=deposit.id, director_account_id=loan.id,
                                           discrepancy_bank_account_id=bank.id)

    resp = post(client, auth, CODES, {"entity_id": connected.id, "expense_codes": ["400"],
                                      "mapping": {"director": "", "pettycash": ""}})

    assert resp.status_code == 200
    row = EntityPettycashSettings.objects.get(entity_id=connected.id)
    assert (row.bank_account_id, row.director_account_id, row.pettycash_account_id,
            row.discrepancy_bank_account_id) == (deposit.id, None, None, None)


@pytest.mark.django_db
@pytest.mark.parametrize("body, error", [
    ({"expense_codes": "400"}, "expense_codes must be an array"),
    ({"expense_codes": []}, "Pick at least one account code."),
    ({"expense_codes": ["400"], "mapping": {"pettycash": "a-bank1", "deposit": "a-bank1"}},
     "Petty Cash Account and Deposit Bank Account cannot be the same."),
])
def test_a_save_that_does_not_add_up_is_refused(client, auth, connected, xero, body, error):
    stored(connected, "a-adv")
    resp = post(client, auth, CODES, {"entity_id": connected.id, **body})
    assert (resp.status_code, resp.json()) == (400, {"error": error})


@pytest.mark.django_db
def test_a_choice_xero_still_has_not_synced_is_a_409_naming_it(client, auth, connected, xero):
    stored(connected, "a-adv")
    resp = post(client, auth, CODES, {"entity_id": connected.id, "expense_codes": ["400"],
                                      "mapping": {"pettycash": "a-ghost"}})
    assert resp.status_code == 409
    assert "pettycash (a-ghost)" in resp.json()["error"]
    assert not EntityPettycashSettings.objects.filter(entity_id=connected.id).exists()


@pytest.mark.django_db
def test_a_cashier_may_read_but_not_save(client, connected, xero):
    cashier = as_member(connected, "cashier")
    assert client.get(CODES, {"entity_id": connected.id}, **cashier).status_code == 200
    assert post(client, cashier, CODES, {"entity_id": connected.id, "expense_codes": ["400"]}).status_code == 403


# ---- step 6: contacts --------------------------------------------------------------------------


@pytest.mark.django_db
def test_contacts_are_saved_and_a_new_one_is_synced_first(client, auth, connected, xero):
    XeroContactSync.objects.create(entity_id=connected.id, xero_contact_id="c-1", name="Alpha")
    xero.on_sync = lambda: XeroContactSync.objects.create(entity_id=connected.id, xero_contact_id="c-2", name="Zeta")

    resp = post(client, auth, CONTACTS, {"entity_id": connected.id,
                                         "contacts": {"director": "c-1", "cash_sale": "c-2", "discrepancy": ""}})

    assert (resp.status_code, resp.json()) == (200, {"ok": True})
    assert xero.calls == [("SYNC",)]  # c-2 wasn't stored yet
    row = EntityPettycashSettings.objects.get(entity_id=connected.id)
    ids = dict(XeroContactSync.objects.filter(entity_id=connected.id).values_list("xero_contact_id", "id"))
    assert (row.director_contact_id, row.cash_sale_contact_id, row.discrepancy_contact_id) == (ids["c-1"], ids["c-2"], None)


@pytest.mark.django_db
def test_stored_contacts_need_no_sync_and_a_missing_one_is_a_409(client, auth, connected, xero):
    XeroContactSync.objects.create(entity_id=connected.id, xero_contact_id="c-1", name="Alpha")
    assert post(client, auth, CONTACTS, {"entity_id": connected.id, "contacts": {"director": "c-1"}}).status_code == 200
    assert xero.calls == []
    resp = post(client, auth, CONTACTS, {"entity_id": connected.id, "contacts": {"director": "c-ghost"}})
    assert resp.status_code == 409 and "director (c-ghost)" in resp.json()["error"]


# ---- step 6: a new contact ---------------------------------------------------------------------


@pytest.mark.django_db
def test_a_new_contact_is_made_in_xero_and_stored(client, auth, connected, xero):
    resp = post(client, auth, CREATE, {"entity_id": connected.id, "name": "  New Supplier  "})

    assert (resp.status_code, resp.json()) == (201, {"id": "c-new", "label": "New Supplier"})
    assert xero.calls == [("POST", "Contacts", {"Contacts": [{"Name": "New Supplier", "IsCustomer": False,
                                                              "IsSupplier": True}]})]
    row = XeroContactSync.objects.get(entity_id=connected.id, xero_contact_id="c-new")
    assert (row.name, row.category, row.xero_org_id) == ("New Supplier", "expense_contact", ORG)


@pytest.mark.django_db
def test_a_new_contact_refused_or_unnamed(client, auth, connected, xero):
    assert post(client, auth, CREATE, {"entity_id": connected.id, "name": " "}).json() == {
        "error": "Contact name is required."}
    xero.down = True
    resp = post(client, auth, CREATE, {"entity_id": connected.id, "contact_name": "X"})
    assert (resp.status_code, resp.json()) == (502, {"error": "Failed to create contact in Xero. Please try again."})
    assert not XeroContactSync.objects.filter(entity_id=connected.id).exists()


# ---- step 8: bill codes ------------------------------------------------------------------------


def bill(entity, code, name, *, active=False, default=False):
    return EntityBillAccountXero.objects.create(entity_id=entity.id, account_code=code, account_name=name,
                                                account_type="EXPENSE", xero_account_id=f"a-{code}",
                                                is_active=active, is_default=default, is_deleted=False, sort_order=0)


def ticked(entity, code, name):
    """An expense account of the Xero chart ticked in step 4 (X3, or the old wizard's tick): Payment
    Settings lists only these (owner, 2026-11-11)."""
    account = AccountInfo.objects.create(entity_id=entity.id, xero_account_id=f"a-{code}", xero_code=code, code=code,
                                         name=name, type="EXPENSE", account_type="expense", status="ACTIVE",
                                         record_status="active")
    EntityAccountXero.objects.create(account_id=account.id, name=name, type="EXPENSE", is_active=True)
    return account


@pytest.mark.django_db
def test_the_bill_codes_are_read_sorted_with_the_default(client, auth, connected, xero):
    ticked(connected, "404", "Bank Fees")
    ticked(connected, "40", "Odd")
    bill(connected, "404", "Bank Fees", active=True, default=True)
    bill(connected, "40", "Odd", active=False)
    resp = client.get(BILLS, {"entity_id": connected.id}, **auth)
    assert resp.json() == {"connected": True, "bill_codes": [{"code": "40", "name": "Odd"},
                                                             {"code": "404", "name": "Bank Fees"}],
                           "selected_codes": ["404"], "default_all": False, "default_code": "404"}
    assert xero.calls == []


@pytest.mark.django_db
def test_the_bill_codes_are_step_4s_ticks_with_no_sync(client, auth, connected, xero):
    ticked(connected, "400", "Advertising")
    bill(connected, "404", "Bank Fees", active=True)  # synced from Xero, not ticked in step 4
    body = client.get(BILLS, {"entity_id": connected.id}, **auth).json()
    assert body["bill_codes"] == [{"code": "400", "name": "Advertising"}] and body["default_all"] is True
    assert xero.calls == []


@pytest.mark.django_db
def test_the_bill_codes_save_keeps_one_and_a_ticked_default(client, auth, connected, xero):
    ticked(connected, "400", "Advertising")
    ticked(connected, "404", "Bank Fees")
    bill(connected, "400", "Advertising", active=True)
    bill(connected, "404", "Bank Fees", default=True)

    done = post(client, auth, BILLS, {"entity_id": connected.id, "selected_codes": ["404", "400"], "default_code": "400"})
    assert done.json() == {"ok": True, "selected_codes": ["400", "404"], "default_code": "400"}
    rows = {r.account_code: (r.is_active, r.is_default) for r in EntityBillAccountXero.objects.filter(entity_id=connected.id)}
    assert rows == {"400": (True, True), "404": (True, False)}

    assert post(client, auth, BILLS, {"entity_id": connected.id, "selected_codes": []}).json() == {
        "error": "Pick at least one account code."}
    assert post(client, auth, BILLS, {"entity_id": connected.id, "selected_codes": ["400"],
                                      "default_code": "404"}).json() == {
        "error": "The default account code must be one of the ticked codes."}


@pytest.mark.django_db
def test_a_disconnect_that_kept_the_organisation_is_not_connected(client, auth, connected, xero):
    """A disconnect keeps the organisation (2026-11-10): the bill codes say so, with no sync asked."""
    Entity.objects.filter(id=connected.id).update(status="disconnected")
    read = client.get(BILLS, {"entity_id": connected.id}, **auth)
    assert (read.status_code, read.json()["connected"]) == (409, False)
    saved = post(client, auth, BILLS, {"entity_id": connected.id, "selected_codes": ["400"]})
    assert saved.status_code == 409
    assert xero.calls == []

