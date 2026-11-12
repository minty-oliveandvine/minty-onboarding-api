"""A company's accounts and contacts in the hub's Settings (owner, 2026-11-12): the internal door
accounts-api calls, and ``onboarding/services/chart.py`` behind it - add, edit, delete, with or
without Xero; only what the company made; nothing changes when Xero says no."""

import json
import uuid

import pytest
from minty_db.models import (
    AccountInfo,
    Bill,
    ReportExpense,
    UserEntity,
    XeroContact,
    XeroContactSync,
)
from minty_shared import tokens

from core import xero_client
from onboarding.services import chart

BASE = "/api/internal/chart"
ORG = "11111111-2222-3333-4444-555555555555"


def assertion(user, entity, scope="company-chart"):
    token = tokens.service_assertion("test-secret-for-the-chart-endpoints", scope, entity.id, user_id=str(user.id))
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _secret(settings):
    settings.SECRET_KEY = "test-secret-for-the-chart-endpoints"


def call(client, user, entity, method, path, body=None, scope="company-chart"):
    kwargs = {"content_type": "application/json", "data": json.dumps(body)} if body is not None else {}
    return getattr(client, method)(BASE + path, **kwargs, **assertion(user, entity, scope))


class FakeXero:
    """Records each write; answers with made ids, or refuses with Xero's message."""

    def __init__(self):
        self.sent, self.refuse = [], None

    def write(self, token, org, method, path, body):
        self.sent.append((method, path, body))
        if self.refuse:
            raise xero_client.XeroRefused(self.refuse)
        if path == "Accounts" and method == "PUT":
            return {"Accounts": [{"AccountID": "x-acc-1", **body}]}
        if path == "Contacts" and not body["Contacts"][0].get("ContactID"):
            return {"Contacts": [{"ContactID": "x-con-1", "Name": body["Contacts"][0]["Name"]}]}
        return {}


@pytest.fixture
def xero(monkeypatch):
    fake = FakeXero()
    monkeypatch.setattr(xero_client, "write", fake.write)
    monkeypatch.setattr(chart, "access_token_for", lambda entity_id: "token")
    return fake


@pytest.fixture
def live(entity):
    entity.xero_org_id, entity.status = ORG, "connected"
    entity.save(update_fields=["xero_org_id", "status"])
    return entity


def account(entity, name="Rent", code="469", source="user", xero_id=None, **extra):
    return AccountInfo.objects.create(entity_id=entity.id, name=name, code=code, xero_code=code,
                                      account_type="expense", type="EXPENSE", status="ACTIVE", source=source,
                                      xero_account_id=xero_id, **extra)


def contact(entity, name="Kee Wah", source="user", xero_id=None):
    return XeroContactSync.objects.create(entity_id=entity.id, name=name, source=source, xero_contact_id=xero_id,
                                          xero_org_id=ORG if xero_id else None)


# ---- the door -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_a_minty_backends_assertion_for_this_scope_opens_it(client, user, entity):
    assert client.get(BASE + "/accounts").status_code == 401
    assert call(client, user, entity, "get", "/accounts", scope="xero-sync").status_code == 401
    assert call(client, user, entity, "get", "/accounts").status_code == 200


@pytest.mark.django_db
def test_the_persons_rights_are_checked_again(client, user, entity):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    viewed = call(client, user, entity, "get", "/accounts")  # a cashier views, and is told what not to do
    assert viewed.status_code == 200
    assert viewed.json()["permissions"] == {"add": False, "edit": False, "delete": False}
    resp = call(client, user, entity, "post", "/accounts", {"code": "470", "name": "Fuel", "type": "expense"})
    assert resp.status_code == 403


# ---- accounts ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_chart_reads_what_may_be_done_and_the_next_codes(client, user, entity):
    account(entity, "Rent", "469")
    account(entity, "Wages", "477", source="template")
    account(entity, "Hidden", "090", is_system=True)
    body = call(client, user, entity, "get", "/accounts").json()
    rows = {a["name"]: a for a in body["accounts"]}
    assert set(rows) == {"Rent", "Wages"}  # the system row stays hidden
    # a copy of Minty's is the company's too (owner, 2026-11-12); the source still says where it came from
    assert rows["Rent"]["own"] is True and rows["Wages"]["own"] is True
    assert rows["Wages"]["source"] == "template"
    assert body["next_codes"]["expense"] == "478" and body["xero_live"] is False
    assert {"value": "expense", "label": "Expense"} in body["types"]


@pytest.mark.django_db
def test_without_xero_an_account_is_the_companys_own(client, user, entity):
    resp = call(client, user, entity, "post", "/accounts", {"code": "470", "name": "Fuel", "type": "expense"})
    assert resp.status_code == 201, resp.json()
    made = AccountInfo.objects.get(entity_id=entity.id, name="Fuel")
    assert (made.source, made.xero_account_id, made.code) == ("user", None, "470")


@pytest.mark.django_db
def test_a_live_company_gets_it_in_xero_first(client, user, live, xero):
    resp = call(client, user, live, "post", "/accounts",
                {"code": "470", "name": "Fuel", "type": "expense", "description": "Vans"})
    assert resp.status_code == 201
    assert xero.sent == [("PUT", "Accounts", {"Code": "470", "Name": "Fuel", "Type": "EXPENSE", "Description": "Vans"})]
    assert AccountInfo.objects.get(entity_id=live.id, name="Fuel").xero_account_id == "x-acc-1"


@pytest.mark.django_db
def test_when_xero_says_no_nothing_changes(client, user, live, xero):
    xero.refuse = "Xero didn't accept that: Please enter a unique Code. Change it and try again."
    resp = call(client, user, live, "post", "/accounts", {"code": "470", "name": "Fuel", "type": "expense"})
    assert (resp.status_code, resp.json()["error"]) == (422, xero.refuse)
    assert not AccountInfo.objects.filter(entity_id=live.id, name="Fuel").exists()


@pytest.mark.django_db
def test_a_taken_code_or_a_bad_field_is_refused(client, user, entity):
    account(entity, "Rent", "469")
    taken = call(client, user, entity, "post", "/accounts", {"code": "469", "name": "Fuel", "type": "expense"})
    assert taken.status_code == 409 and taken.json()["fields"]["code"].startswith("The code 469 is already used")
    bad = call(client, user, entity, "post", "/accounts", {"code": "4 7", "name": "Fuel", "type": "expense"})
    assert bad.status_code == 422 and "letters, numbers" in bad.json()["fields"]["code"]


@pytest.mark.django_db
def test_a_copy_of_mintys_account_is_the_companys_to_edit_and_delete(client, user, entity):
    """Owner, 2026-11-12: the company's copies of Minty's accounts are its own; only the hidden
    system rows stay locked."""
    wages = account(entity, "Wages", "477", source="template")
    edit = call(client, user, entity, "patch", f"/accounts/{wages.id}",
                {"code": "477", "name": "Pay", "type": "expense"})
    assert edit.status_code == 200 and AccountInfo.objects.get(id=wages.id).name == "Pay"
    assert call(client, user, entity, "delete", f"/accounts/{wages.id}").status_code == 200
    hidden = account(entity, "Petty Cash", "090", source="template", is_system=True)
    assert call(client, user, entity, "delete", f"/accounts/{hidden.id}").status_code == 404


@pytest.mark.django_db
def test_an_xero_account_is_the_companys_and_changes_in_xero_too(client, user, live, xero):
    rent = account(live, "Rent", "469", source="xero", xero_id="x-rent")
    resp = call(client, user, live, "patch", f"/accounts/{rent.id}", {"code": "469", "name": "Rent HK", "type": "expense"})
    assert resp.status_code == 200
    assert xero.sent == [("POST", "Accounts/x-rent", {"Code": "469", "Name": "Rent HK", "Type": "EXPENSE"})]
    assert AccountInfo.objects.get(id=rent.id).name == "Rent HK"


@pytest.mark.django_db
def test_a_linked_account_waits_for_a_reconnect(client, user, entity):
    entity.xero_org_id, entity.status = ORG, "disconnected"
    entity.save(update_fields=["xero_org_id", "status"])
    rent = account(entity, "Rent", "469", source="xero", xero_id="x-rent")
    resp = call(client, user, entity, "delete", f"/accounts/{rent.id}")
    assert resp.status_code == 409 and resp.json()["error"].startswith("Rent is in Xero. Reconnect Xero")
    assert AccountInfo.objects.filter(id=rent.id).exists()


@pytest.mark.django_db
def test_delete_removes_an_unused_account_and_archives_a_used_one_in_xero_too(client, user, live, xero):
    spare = account(live, "Spare", "471")
    gone = call(client, user, live, "delete", f"/accounts/{spare.id}")
    assert gone.json() == {"archived": False, "message": "Spare deleted."}
    assert not AccountInfo.objects.filter(id=spare.id).exists()

    rent = account(live, "Rent", "469", source="xero", xero_id="x-rent")
    Bill.objects.create(entity_id=live.id, reference="B-1")
    from minty_db.models import BillLine

    BillLine.objects.create(bill=Bill.objects.get(reference="B-1"), account_id=rent.id, account_code="469")
    kept = call(client, user, live, "delete", f"/accounts/{rent.id}")
    assert kept.json()["archived"] is True
    assert xero.sent[-1] == ("POST", "Accounts/x-rent", {"Status": "ARCHIVED"})
    assert AccountInfo.objects.get(id=rent.id).record_status == "archived"
    names = [a["name"] for a in call(client, user, live, "get", "/accounts").json()["accounts"]]
    assert "Rent" not in names


# ---- contacts ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_without_xero_a_contact_is_the_companys_own_and_a_taken_name_is_refused(client, user, entity):
    resp = call(client, user, entity, "post", "/contacts", {"name": "  Kee   Wah "})
    assert resp.status_code == 201 and resp.json()["contact"]["name"] == "Kee Wah"
    made = XeroContactSync.objects.get(entity_id=entity.id, name="Kee Wah")
    assert (made.source, made.xero_contact_id) == ("user", None)
    again = call(client, user, entity, "post", "/contacts", {"name": "kee wah"})
    assert again.status_code == 409
    assert again.json()["error"] == "Kee Wah is already in your contacts. Use that one, or choose another name."


@pytest.mark.django_db
def test_a_live_companys_contact_links_to_xeros_of_that_name_else_is_made_there(client, user, live, xero):
    XeroContact.objects.create(entity_id=live.id, xero_org_id=ORG, xero_guid="x-sam", name="Sam's Fish",
                               status="ACTIVE")
    linked = call(client, user, live, "post", "/contacts", {"name": "SAM'S FISH"})
    assert linked.status_code == 201 and xero.sent == []  # no duplicate in Xero
    assert XeroContactSync.objects.get(entity_id=live.id, name="SAM'S FISH").xero_contact_id == "x-sam"
    assert linked.json()["contact"]["xero_id"] == "x-sam"

    call(client, user, live, "post", "/contacts", {"name": "Kee Wah"})
    assert xero.sent == [("POST", "Contacts", {"Contacts": [{"Name": "Kee Wah", "IsCustomer": False,
                                                             "IsSupplier": True}]})]
    assert XeroContactSync.objects.get(entity_id=live.id, name="Kee Wah").xero_contact_id == "x-con-1"


@pytest.mark.django_db
def test_a_contact_renames_and_deletes_in_xero_too_and_minty_ones_are_locked(client, user, live, xero):
    kee = contact(live, "Kee Wah", source="xero", xero_id="x-kee")
    renamed = call(client, user, live, "patch", f"/contacts/{kee.id}", {"name": "Kee Wah Bakery"})
    assert renamed.status_code == 200
    assert xero.sent[-1] == ("POST", "Contacts", {"Contacts": [{"ContactID": "x-kee", "Name": "Kee Wah Bakery"}]})

    gone = call(client, user, live, "delete", f"/contacts/{kee.id}")
    assert gone.json()["archived"] is False and not XeroContactSync.objects.filter(id=kee.id).exists()
    assert xero.sent[-1] == ("POST", "Contacts", {"Contacts": [{"ContactID": "x-kee", "ContactStatus": "ARCHIVED"}]})

    template = contact(live, "Walk-in Customer", source="template")
    assert call(client, user, live, "delete", f"/contacts/{template.id}").status_code == 200  # the company's copy


@pytest.mark.django_db
def test_a_used_contact_is_archived_and_leaves_the_list(client, user, entity):
    kee = contact(entity)
    from minty_db.models import Report

    report = Report.objects.create(entity_id=entity.id, transaction_date="2026-11-01")
    ReportExpense.objects.create(report_id=report.id, contact_id=kee.id, amount=1)
    assert call(client, user, entity, "delete", f"/contacts/{kee.id}").json()["archived"] is True
    assert XeroContactSync.objects.get(id=kee.id).record_status == "archived"
    assert call(client, user, entity, "get", "/contacts").json()["contacts"] == []


@pytest.mark.django_db
def test_an_unknown_id_is_not_found(client, user, entity):
    for path in (f"/accounts/{uuid.uuid4()}", "/accounts/nope", f"/contacts/{uuid.uuid4()}"):
        assert call(client, user, entity, "delete", path).status_code == 404


# ---- Minty's accounts (owner, 2026-11-12: "I should be able to add existing ones from Minty") ----


def template(code, name, kind="expense", **extra):
    from minty_db.models import AccountTemplate

    return AccountTemplate.objects.create(code=code, name=name, type=kind, **extra)


@pytest.mark.django_db
def test_the_read_offers_mintys_accounts_the_company_has_not_got(client, user, entity):
    account(entity, "Rent", "469", source="template")
    template("469", "Rent")
    fuel = template("470", "Fuel", description="Vans and cars")
    template("090", "Petty Cash", kind="bank", pettycash_role="petty_cash")  # a hidden system row

    offered = call(client, user, entity, "get", "/accounts").json()["minty_accounts"]
    assert offered == [{"id": str(fuel.id), "code": "470", "name": "Fuel", "type": "expense",
                        "type_label": "Expense", "description": "Vans and cars"}]


@pytest.mark.django_db
def test_a_minty_account_is_added_as_mintys_copy(client, user, entity):
    fuel = template("470", "Fuel")
    resp = call(client, user, entity, "post", "/accounts", {"template_id": str(fuel.id)})
    assert resp.status_code == 201, resp.json()
    made = AccountInfo.objects.get(entity_id=entity.id, name="Fuel")
    assert (made.code, made.source, made.account_type) == ("470", "template", "expense")
    assert resp.json()["account"]["own"] is True  # the company's copy, its own to change (owner, 2026-11-12)
    again = call(client, user, entity, "post", "/accounts", {"template_id": str(fuel.id)})
    assert again.status_code == 404  # no longer offered


@pytest.mark.django_db
def test_a_minty_account_whose_code_is_taken_is_refused_saying_so(client, user, entity):
    account(entity, "Diesel", "470")
    fuel = template("470", "Fuel")
    resp = call(client, user, entity, "post", "/accounts", {"template_id": str(fuel.id)})
    assert resp.status_code == 409
    assert resp.json()["error"] == "The code 470 is already used by Diesel. Choose another code."


@pytest.mark.django_db
def test_a_live_company_gets_mintys_account_in_xero_first(client, user, live, xero):
    fuel = template("470", "Fuel")
    assert call(client, user, live, "post", "/accounts", {"template_id": str(fuel.id)}).status_code == 201
    assert xero.sent == [("PUT", "Accounts", {"Code": "470", "Name": "Fuel", "Type": "EXPENSE"})]
    assert AccountInfo.objects.get(entity_id=live.id, name="Fuel").xero_account_id == "x-acc-1"


# ---- deleted, then added again: brought back, never a twin (owner, 2026-11-12) -------------------


def xero_contact_copy(entity, guid, name, status="ACTIVE"):
    return XeroContact.objects.create(entity_id=entity.id, xero_org_id=ORG, xero_guid=guid, name=name,
                                      status=status)


@pytest.mark.django_db
def test_a_deleted_unused_contact_added_again_is_unarchived_in_xero(client, user, live, xero):
    kee = contact(live, "Kee Wah", source="xero", xero_id="x-kee")
    xero_contact_copy(live, "x-kee", "Kee Wah")
    call(client, user, live, "delete", f"/contacts/{kee.id}")
    # Minty's copy says archived at once, so nothing links to it meanwhile
    assert XeroContact.objects.get(entity_id=live.id, xero_guid="x-kee").status == "ARCHIVED"

    again = call(client, user, live, "post", "/contacts", {"name": "kee wah"})
    assert again.status_code == 201 and again.json()["message"] == "Kee Wah is back."
    assert xero.sent[-1] == ("POST", "Contacts", {"Contacts": [{"ContactID": "x-kee", "ContactStatus": "ACTIVE"}]})
    assert XeroContactSync.objects.get(entity_id=live.id, record_status="active").xero_contact_id == "x-kee"
    assert XeroContact.objects.get(entity_id=live.id, xero_guid="x-kee").status == "ACTIVE"
    assert not [s for s in xero.sent if s[2].get("Contacts", [{}])[0].get("IsSupplier")]  # never made anew


@pytest.mark.django_db
def test_a_deleted_used_contact_added_again_is_the_same_row_back(client, user, live, xero):
    from minty_db.models import Report

    kee = contact(live, "Kee Wah", source="xero", xero_id="x-kee")
    report = Report.objects.create(entity_id=live.id, transaction_date="2026-11-01")
    ReportExpense.objects.create(report_id=report.id, contact_id=kee.id, amount=1)
    call(client, user, live, "delete", f"/contacts/{kee.id}")

    again = call(client, user, live, "post", "/contacts", {"name": "Kee Wah"})
    assert again.status_code == 201 and again.json()["contact"]["id"] == str(kee.id)
    kee.refresh_from_db()
    assert kee.record_status == "active"
    assert xero.sent[-1] == ("POST", "Contacts", {"Contacts": [{"ContactID": "x-kee", "ContactStatus": "ACTIVE"}]})


@pytest.mark.django_db
def test_offline_a_minty_only_contact_comes_back_but_a_linked_one_waits_for_xero(client, user, entity):
    from minty_db.models import Report

    report = Report.objects.create(entity_id=entity.id, transaction_date="2026-11-01")
    own = contact(entity, "Kee Wah")
    linked = contact(entity, "Sam's Fish", source="xero", xero_id="x-sam")
    for row in (own, linked):
        ReportExpense.objects.create(report_id=report.id, contact_id=row.id, amount=1)
        XeroContactSync.objects.filter(id=row.id).update(record_status="archived")

    back = call(client, user, entity, "post", "/contacts", {"name": "Kee Wah"})
    assert back.status_code == 201 and back.json()["contact"]["id"] == str(own.id)
    waits = call(client, user, entity, "post", "/contacts", {"name": "Sam's Fish"})
    assert waits.status_code == 409 and "Reconnect Xero to bring it back" in waits.json()["error"]


@pytest.mark.django_db
def test_a_deleted_used_account_added_again_is_the_same_row_back_and_active_in_xero(client, user, live, xero):
    rent = account(live, "Rent", "469", source="xero", xero_id="x-rent")
    Bill.objects.create(entity_id=live.id, reference="B-1")
    from minty_db.models import AccountModule, BillLine

    BillLine.objects.create(bill=Bill.objects.get(reference="B-1"), account_id=rent.id, account_code="469")
    AccountModule.objects.create(account_id=rent.id, entity_id=live.id, module_code="PETTY_CASH", is_active=True)
    call(client, user, live, "delete", f"/accounts/{rent.id}")
    assert AccountModule.objects.get(account_id=rent.id).is_active is False

    again = call(client, user, live, "post", "/accounts", {"code": "469", "name": "rent", "type": "expense"})
    assert again.status_code == 201 and again.json()["account"]["id"] == str(rent.id)
    assert again.json()["message"] == "Rent is back."
    assert xero.sent[-1] == ("POST", "Accounts/x-rent", {"Status": "ACTIVE"})
    rent.refresh_from_db()
    assert (rent.record_status, rent.status) == ("active", "ACTIVE")
    assert AccountModule.objects.get(account_id=rent.id, module_code="PETTY_CASH").is_active is True


@pytest.mark.django_db
def test_a_deleted_unused_account_added_again_is_unarchived_in_xero(client, user, live, xero):
    from minty_db.models import XeroAccount

    spare = account(live, "Spare", "471", source="xero", xero_id="x-spare")
    XeroAccount.objects.create(entity_id=live.id, xero_org_id=ORG, xero_guid="x-spare", code="471", name="Spare",
                               xero_type="EXPENSE")
    call(client, user, live, "delete", f"/accounts/{spare.id}")
    assert not AccountInfo.objects.filter(id=spare.id).exists()
    assert XeroAccount.objects.get(xero_guid="x-spare").status == "ARCHIVED"

    again = call(client, user, live, "post", "/accounts", {"code": "471", "name": "Spare", "type": "expense"})
    assert again.status_code == 201, again.json()
    assert xero.sent[-1] == ("POST", "Accounts/x-spare", {"Status": "ACTIVE"})
    assert not [s for s in xero.sent if s[0] == "PUT"]  # never made anew
    assert AccountInfo.objects.get(entity_id=live.id, name="Spare").xero_account_id == "x-spare"
    assert XeroAccount.objects.get(xero_guid="x-spare").status == "ACTIVE"
