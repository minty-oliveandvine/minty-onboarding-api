"""Accounting Setup - wizard step 4 with Xero optional (Figma 348:1007; services/accounting.py).

The Minty path: how Minty keeps the books (4.1), the business answers (4.2), the company's own
sales and expense accounts with Auto Fill (4.3, 4.4) and the five accounts daily closing needs
(4.5). An account made here is written both ways while the apps still read the Xero-shaped
columns: Minty's own and the old ones, plus Petty Cash's tick and Payment Request's bill code for
an expense account.
"""

import uuid
from datetime import date

import pytest
from minty_db.models import (
    AccountInfo,
    AccountModule,
    AccountTemplate,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    Report,
    ReportExpense,
    User,
    UserEntity,
)

from onboarding.services import accounting, state
from tests.conftest import make_token, requires_postgres

BASE = "/api/onboarding/accounting"


def post(client, auth, path, payload):
    return client.post(BASE + path, payload, content_type="application/json", **auth)


def patch(client, auth, path, payload):
    return client.patch(BASE + path, payload, content_type="application/json", **auth)


@pytest.fixture
def templates(db, countries):
    """The shared system rows, an F&B-with-cash set, a Retail set and the General set."""
    rows = [
        (None, None, "090", "Petty Cash", "bank", "petty_cash"),
        (None, None, "091", "Bank Account", "bank", "bank"),
        (None, None, "800", "Amount Due to Director", "current_liability", "director"),
        ("fnb", True, "200", "Food sales", "sales", None),
        ("fnb", True, "300", "Food and drink costs", "direct_costs", None),
        ("fnb", True, "420", "Cleaning", "expense", None),
        ("retail", True, "201", "Product sales", "sales", None),
        ("general", None, "260", "Other income", "other_income", None),
        ("general", None, "404", "Bank fees", "expense", None),
    ]
    return {
        name: AccountTemplate.objects.create(
            business_type=bt,
            uses_sales_cash=cash,
            code=code,
            name=name,
            type=typ,
            pettycash_role=role,
            description=f"About {name}",
        )
        for bt, cash, code, name, typ, role in rows
    }


@pytest.fixture
def minty(client, auth, entity):
    """The company keeps its books in Minty (4.1)."""
    assert (
        post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"}).status_code
        == 200
    )
    entity.refresh_from_db()
    return entity


def _answer(client, auth, entity, business="fnb", cash=True):
    return post(
        client,
        auth,
        "/business",
        {"entity_id": str(entity.id), "business_type": business, "uses_sales_cash": cash},
    )


def _setup(client, auth, entity):
    resp = client.get(BASE, {"entity_id": str(entity.id)}, **auth)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _add(client, auth, entity, **fields):
    return post(client, auth, "/accounts", {"entity_id": str(entity.id), **fields})


# ---- 4.1: how Minty keeps the books ----------------------------------------------------------------


@pytest.mark.django_db
def test_choosing_minty_on_its_own(client, auth, entity):
    resp = post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"})
    assert resp.status_code == 200 and resp.json()["mode"] == "minty"
    entity.refresh_from_db()
    assert entity.setup_mode == "minty"


@pytest.mark.django_db
def test_an_unknown_mode_is_refused_with_a_sentence(client, auth, entity):
    resp = post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "sage"})
    assert resp.status_code == 400
    assert resp.json()["fields"]["mode"] == "Choose how Minty should work."


@pytest.mark.django_db
def test_a_connected_company_cannot_switch_to_minty_on_its_own(client, auth, entity):
    Entity.objects.filter(id=entity.id).update(xero_org_id="ORG-1")
    resp = post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "minty"})
    assert resp.status_code == 409 and "Disconnect Xero first" in resp.json()["error"]


@pytest.mark.django_db
def test_switching_to_xero_clears_the_accounts_minty_set_up(client, auth, minty, templates):
    """Onboarding's choice is free (owner, 2026-11-11): Xero takes the place of Minty's own chart, so
    every account Minty made - the business type's set, Auto Fill's and the person's own - goes,
    with the daily-closing choices that pointed at them. The system rows stay, unseen."""
    _answer(client, auth, minty)
    ids = [str(templates["Cleaning"].id)]
    post(
        client,
        auth,
        "/autofill",
        {"entity_id": str(minty.id), "kind": "expense", "template_ids": ids},
    )
    rent = _add(client, auth, minty, code="499", name="Sundries", type="expense").json()["account"]
    EntityPettycashSettings.objects.filter(entity_id=minty.id).update(
        discrepancy_account_id=rent["id"]
    )
    system = set(
        AccountInfo.objects.filter(entity_id=minty.id, is_system=True).values_list("id", flat=True)
    )

    resp = post(client, auth, "/mode", {"entity_id": str(minty.id), "mode": "xero"})

    assert resp.status_code == 200, resp.content
    assert resp.json()["mode"] == "xero"
    left = AccountInfo.objects.filter(entity_id=minty.id)
    assert set(left.values_list("id", flat=True)) == system
    assert EntityPettycashSettings.objects.get(entity_id=minty.id).discrepancy_account_id is None
    assert (
        not AccountModule.objects.filter(entity_id=minty.id).exclude(account_id__in=system).exists()
    )


@pytest.mark.django_db
def test_xero_is_refused_once_a_report_uses_minty_s_accounts(client, auth, minty):
    """Nothing a report or bill already points at is deleted: that company stays on Minty here and
    connects Xero from Settings, where its accounts are matched."""
    rent = _add(client, auth, minty, code="499", name="Sundries", type="expense").json()["account"]
    report = Report.objects.create(
        id=str(uuid.uuid4()),
        entity_id=minty.id,
        status="draft",
        transaction_date=date.today(),
        opening_balance=0.0,
        cash_addition=0.0,
    )
    ReportExpense.objects.create(
        report_id=report.id, item="Sundries", amount=5, account_id=rent["id"]
    )

    resp = post(client, auth, "/mode", {"entity_id": str(minty.id), "mode": "xero"})

    assert resp.status_code == 409
    assert resp.json()["error"] == (
        "This company's reports or bills already use its accounts in Minty, so it keeps using Minty "
        "on its own for now. Connect Xero from Settings once setup is finished."
    )
    assert AccountInfo.objects.filter(id=rent["id"]).exists()
    assert Entity.objects.get(id=minty.id).setup_mode == "minty"


# ---- 4.2: the business answers ---------------------------------------------------------------------


@pytest.mark.django_db
def test_the_cash_question_must_be_answered(client, auth, minty):
    resp = post(client, auth, "/business", {"entity_id": str(minty.id), "business_type": "fnb"})
    assert resp.status_code == 400
    assert resp.json()["fields"] == {
        "uses_sales_cash": "Choose how you handle cash sales to continue."
    }


@pytest.mark.django_db
def test_answering_adds_the_shared_system_accounts_hidden_and_chosen(
    client, auth, minty, templates
):
    resp = _answer(client, auth, minty)
    assert resp.status_code == 200 and resp.json()["business_answered"] is True
    system = AccountInfo.objects.filter(entity_id=minty.id, is_system=True)
    assert sorted(system.values_list("code", flat=True)) == ["090", "091", "800"]
    assert all(a.is_system and a.source == "template" and a.xero_account_id is None for a in system)
    settings = EntityPettycashSettings.objects.get(entity_id=minty.id)
    assert settings.pettycash_account.code == "090" and settings.bank_account.code == "091"
    assert settings.director_account.code == "800"
    body = _setup(client, auth, minty)
    # the set's sales accounts come with the answer (owner, 2026-11-11: no sales screen); the expense
    # list starts empty for 4.4's Auto Fill
    assert [a["name"] for a in body["accounts"]["sales"]] == ["Food sales"]
    assert body["accounts"]["expense"] == []
    assert body["slots"]["petty_cash"]["account"]["name"] == "Petty Cash"
    assert body["slots_ready"] == 3
    assert not EntityAccountXero.objects.filter(account__entity_id=minty.id).exists(), (
        "hidden: no Petty Cash tick"
    )
    _answer(client, auth, minty)
    assert AccountInfo.objects.filter(entity_id=minty.id).count() == 4, (
        "answering again adds nothing"
    )


@pytest.mark.django_db
def test_none_of_these_is_an_empty_business_type(client, auth, minty, templates):
    resp = _answer(client, auth, minty, business=None, cash=True)
    assert resp.status_code == 200
    minty.refresh_from_db()
    assert minty.business_type is None and minty.uses_sales_cash is True
    assert AccountInfo.objects.filter(entity_id=minty.id, is_system=True).count() == 3


@pytest.mark.django_db
def test_the_xero_path_answers_without_adding_accounts(client, auth, entity, templates):
    post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "xero"})
    assert _answer(client, auth, entity).status_code == 200
    assert not AccountInfo.objects.filter(entity_id=entity.id).exists()


# ---- Auto Fill (4.3, 4.4) --------------------------------------------------------------------------


@pytest.mark.django_db
def test_auto_fill_offers_the_companys_set(client, auth, minty, templates):
    _answer(client, auth, minty, business="fnb", cash=True)
    resp = client.get(BASE + "/autofill", {"entity_id": str(minty.id), "kind": "expense"}, **auth)
    assert resp.status_code == 200
    names = [o["name"] for o in resp.json()["options"]]
    assert names == ["Food and drink costs", "Cleaning"], (
        "its own set only: General is a set of its own"
    )
    groups = {o["name"]: o["group"] for o in resp.json()["options"]}
    assert (
        groups["Food and drink costs"] == "cost_of_sales" and groups["Cleaning"] == "running_costs"
    )


@pytest.mark.django_db
def test_none_of_these_auto_fill_offers_the_general_set(client, auth, minty, templates):
    _answer(client, auth, minty, business=None, cash=True)
    resp = client.get(BASE + "/autofill", {"entity_id": str(minty.id), "kind": "sales"}, **auth)
    assert resp.json()["based_on"] == "general"
    assert [o["name"] for o in resp.json()["options"]] == ["Other income"]


@pytest.mark.django_db
def test_auto_fill_adds_the_chosen_rows_and_revert_takes_them_out(client, auth, minty, templates):
    _answer(client, auth, minty)
    ids = [str(templates["Cleaning"].id), str(templates["Food and drink costs"].id)]
    resp = post(
        client,
        auth,
        "/autofill",
        {"entity_id": str(minty.id), "kind": "expense", "template_ids": ids},
    )
    assert resp.status_code == 200 and len(resp.json()["added"]) == 2
    cleaning = AccountInfo.objects.get(entity_id=minty.id, code="420")
    assert (cleaning.source, cleaning.type, cleaning.xero_code, cleaning.status) == (
        "template",
        "EXPENSE",
        "420",
        "ACTIVE",
    )
    again = post(
        client,
        auth,
        "/autofill",
        {"entity_id": str(minty.id), "kind": "expense", "template_ids": ids},
    )
    assert again.json()["added"] == [], "already there: left alone"

    used = AccountInfo.objects.get(entity_id=minty.id, code="300")
    EntityPettycashSettings.objects.filter(entity_id=minty.id).update(
        discrepancy_account_id=used.id
    )
    resp = post(client, auth, "/autofill/revert", {"entity_id": str(minty.id), "kind": "expense"})
    assert resp.json()["removed"] == 1 and [k["name"] for k in resp.json()["kept"]] == [
        "Food and drink costs"
    ]
    assert not AccountInfo.objects.filter(entity_id=minty.id, code="420").exists()
    assert not EntityBillAccountXero.objects.filter(entity_id=minty.id, account_code="420").exists()

    # What stays is no longer Auto Fill's to take out: the screen reads `auto_filled` (2026-11-11,
    # the owner: a kept Cash Over / Short made Revert look as if it filled the list again)
    rows = {r["name"]: r for r in _setup(client, auth, minty)["accounts"]["expense"]}
    assert rows["Food and drink costs"]["source"] == "template"
    assert rows["Food and drink costs"]["auto_filled"] is False


@pytest.mark.django_db
def test_an_auto_fill_row_nothing_uses_is_marked_auto_filled(client, auth, minty, templates):
    _answer(client, auth, minty)
    ids = [str(templates["Cleaning"].id)]
    post(
        client,
        auth,
        "/autofill",
        {"entity_id": str(minty.id), "kind": "expense", "template_ids": ids},
    )
    _add(client, auth, minty, code="499", name="Sundries", type="expense")

    rows = {r["name"]: r for r in _setup(client, auth, minty)["accounts"]["expense"]}
    assert rows["Cleaning"]["auto_filled"] is True
    assert rows["Sundries"]["auto_filled"] is False


@pytest.mark.django_db
def test_auto_fill_refuses_a_row_whose_code_the_company_already_uses(
    client, auth, minty, templates
):
    _answer(client, auth, minty)
    _add(client, auth, minty, code="420", name="Repairs", type="expense")
    resp = post(
        client,
        auth,
        "/autofill",
        {
            "entity_id": str(minty.id),
            "kind": "expense",
            "template_ids": [str(templates["Cleaning"].id)],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "The code 420 is already used by Repairs. Choose another code."


@pytest.mark.django_db
def test_auto_fill_refuses_a_row_from_another_set(client, auth, minty, templates):
    _answer(client, auth, minty, business="fnb")
    resp = post(
        client,
        auth,
        "/autofill",
        {
            "entity_id": str(minty.id),
            "kind": "sales",
            "template_ids": [str(templates["Product sales"].id)],
        },
    )
    assert resp.status_code == 400


# ---- one account -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_expense_account_is_written_for_the_old_readers_too(client, auth, minty):
    resp = _add(
        client, auth, minty, code="405", name="Rent", type="expense", description="Shop rent"
    )
    assert resp.status_code == 201
    row = resp.json()["account"]
    assert (row["code"], row["kind"], row["group"], row["source"]) == (
        "405",
        "expense",
        "running_costs",
        "user",
    )
    account = AccountInfo.objects.get(id=row["id"])
    assert (account.type, account.xero_code, account.status, account.account_type) == (
        "EXPENSE",
        "405",
        "ACTIVE",
        "expense",
    )
    assert EntityAccountXero.objects.get(account_id=account.id).is_active is True
    bill = EntityBillAccountXero.objects.get(entity_id=minty.id, account_code="405")
    assert (bill.xero_account_id, bill.account_name, bill.is_active) == ("", "Rent", True)
    assert set(
        AccountModule.objects.filter(account_id=account.id).values_list("module_code", flat=True)
    ) == {"PETTY_CASH", "PAYMENT_REQUEST"}


@pytest.mark.django_db
def test_a_sales_account_has_no_expense_rows(client, auth, minty):
    resp = _add(client, auth, minty, code="200", name="Sales", type="sales")
    assert resp.status_code == 201
    assert not EntityAccountXero.objects.filter(account_id=resp.json()["account"]["id"]).exists()
    assert not EntityBillAccountXero.objects.filter(entity_id=minty.id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fields", "field", "sentence"),
    [
        (
            {"name": "Rent", "type": "expense"},
            "code",
            "The code is still empty. Add a code to continue.",
        ),
        (
            {"code": "405", "type": "expense"},
            "name",
            "The name is still empty. Add a name to continue.",
        ),
        ({"code": "405", "name": "Rent"}, "type", "Choose the account type to continue."),
        (
            {"code": "40512345678", "name": "Rent", "type": "expense"},
            "code",
            "The code can be up to 10 characters. Shorten it to continue.",
        ),
        (
            {"code": "4 05", "name": "Rent", "type": "expense"},
            "code",
            "The code can use letters, numbers, dots and dashes. Remove the other characters.",
        ),
        (
            {"code": "405", "name": "R" * 81, "type": "expense"},
            "name",
            "The name can be up to 80 characters. Shorten it to continue.",
        ),
    ],
)
def test_each_field_is_checked_with_a_sentence(client, auth, minty, fields, field, sentence):
    resp = _add(client, auth, minty, **fields)
    assert resp.status_code == 400
    assert resp.json()["fields"][field] == sentence


@pytest.mark.django_db
def test_a_bank_account_may_have_no_code(client, auth, minty):
    assert _add(client, auth, minty, name="Cash box", type="bank").status_code == 201


@pytest.mark.django_db
def test_a_code_or_name_another_account_has_is_a_409(client, auth, minty):
    _add(client, auth, minty, code="405", name="Rent", type="expense")
    same_code = _add(client, auth, minty, code="405", name="Premises", type="expense")
    assert same_code.status_code == 409
    assert (
        same_code.json()["fields"]["code"]
        == "The code 405 is already used by Rent. Choose another code."
    )
    same_name = _add(client, auth, minty, code="406", name="  rent ", type="expense")
    assert same_name.status_code == 409
    assert (
        same_name.json()["fields"]["name"]
        == "Another account is already called Rent. Choose another name."
    )


@pytest.mark.django_db
def test_the_next_free_code_follows_the_type(client, auth, minty):
    _add(client, auth, minty, code="270", name="Other income", type="revenue")
    body = _setup(client, auth, minty)
    assert body["next_code"]["revenue"] == "271"
    assert body["next_code"]["bank"] == "090"
    assert body["next_code"]["sales"] == "200"


@pytest.mark.django_db
def test_editing_an_account_moves_its_bill_code_too(client, auth, minty):
    account_id = _add(client, auth, minty, code="405", name="Rent", type="expense").json()[
        "account"
    ]["id"]
    resp = patch(
        client,
        auth,
        f"/accounts/{account_id}",
        {"entity_id": str(minty.id), "code": "469", "name": "Shop rent", "type": "overheads"},
    )
    assert resp.status_code == 200 and resp.json()["account"]["code"] == "469"
    bill = EntityBillAccountXero.objects.get(entity_id=minty.id, xero_account_id="")
    assert (bill.account_code, bill.account_name, bill.account_type) == (
        "469",
        "Shop rent",
        "OVERHEADS",
    )
    assert EntityAccountXero.objects.get(account_id=account_id).name == "Shop rent"


@pytest.mark.django_db
def test_changing_the_type_clears_a_slot_it_no_longer_fits(client, auth, minty):
    account_id = _add(
        client,
        auth,
        minty,
        code="480",
        name="Cash over / short",
        type="expense",
        role="discrepancy",
    ).json()["account"]["id"]
    assert (
        str(EntityPettycashSettings.objects.get(entity_id=minty.id).discrepancy_account_id)
        == account_id
    )
    patch(
        client,
        auth,
        f"/accounts/{account_id}",
        {"entity_id": str(minty.id), "code": "480", "name": "Cash over / short", "type": "revenue"},
    )
    assert EntityPettycashSettings.objects.get(entity_id=minty.id).discrepancy_account_id is None


@pytest.mark.django_db
def test_an_account_from_xero_is_changed_in_xero(client, auth, minty):
    account = AccountInfo.objects.create(
        entity_id=minty.id,
        name="Rent",
        type="EXPENSE",
        xero_account_id="g-1",
        code="469",
        account_type="expense",
    )
    resp = patch(
        client,
        auth,
        f"/accounts/{account.id}",
        {"entity_id": str(minty.id), "code": "470", "name": "Rent", "type": "expense"},
    )
    assert resp.status_code == 409 and "Change it in Xero" in resp.json()["error"]


@pytest.mark.django_db
def test_removing_an_unused_account_deletes_it_and_a_used_one_is_archived(
    client, auth, minty, entity
):
    unused = _add(client, auth, minty, code="405", name="Rent", type="expense").json()["account"][
        "id"
    ]
    resp = client.delete(f"{BASE}/accounts/{unused}?entity_id={minty.id}", **auth)
    assert resp.status_code == 200 and resp.json() == {"archived": False}
    assert not AccountInfo.objects.filter(id=unused).exists()
    assert not EntityBillAccountXero.objects.filter(entity_id=minty.id).exists()

    used = _add(client, auth, minty, code="410", name="Utilities", type="expense").json()[
        "account"
    ]["id"]
    EntityPettycashSettings.objects.update_or_create(
        entity_id=minty.id, defaults={"discrepancy_account_id": used}
    )
    resp = client.delete(f"{BASE}/accounts/{used}?entity_id={minty.id}", **auth)
    assert resp.json() == {"archived": True}
    account = AccountInfo.objects.get(id=used)
    assert (account.record_status, account.status) == ("archived", "ARCHIVED")
    assert (
        EntityBillAccountXero.objects.get(entity_id=minty.id, account_code="410").is_deleted is True
    )
    assert _setup(client, auth, minty)["accounts"]["expense"] == [], (
        "an archived account leaves the list"
    )


@pytest.mark.django_db
def test_a_system_account_cannot_be_removed(client, auth, minty, templates):
    _answer(client, auth, minty)
    petty = AccountInfo.objects.get(entity_id=minty.id, code="090")
    resp = client.delete(f"{BASE}/accounts/{petty.id}?entity_id={minty.id}", **auth)
    assert resp.status_code == 409


@pytest.mark.django_db
def test_another_companys_account_is_not_found(client, auth, minty, countries):
    other = Entity.objects.create(name="Elsewhere", country_code="HK", status="onboarding")
    theirs = AccountInfo.objects.create(
        entity_id=other.id, name="Rent", type="EXPENSE", code="405", account_type="expense"
    )
    resp = patch(
        client,
        auth,
        f"/accounts/{theirs.id}",
        {"entity_id": str(minty.id), "code": "406", "name": "Mine now", "type": "expense"},
    )
    assert resp.status_code == 404
    assert AccountInfo.objects.get(id=theirs.id).name == "Rent"
    assert (
        client.delete(f"{BASE}/accounts/not-a-uuid?entity_id={minty.id}", **auth).status_code == 404
    )


# ---- 4.5: the five accounts daily closing needs ------------------------------------------------------


@pytest.mark.django_db
def test_choosing_and_clearing_a_daily_closing_account(client, auth, minty):
    account_id = _add(client, auth, minty, code="201", name="Product sales", type="sales").json()[
        "account"
    ]["id"]
    resp = post(
        client,
        auth,
        "/slots",
        {"entity_id": str(minty.id), "role": "cash_sale", "account_id": account_id},
    )
    assert resp.status_code == 200 and resp.json()["account"]["name"] == "Product sales"
    assert _setup(client, auth, minty)["slots"]["cash_sale"]["account"]["id"] == account_id
    resp = post(
        client,
        auth,
        "/slots",
        {"entity_id": str(minty.id), "role": "cash_sale", "account_id": None},
    )
    assert resp.json()["account"] is None
    assert EntityPettycashSettings.objects.get(entity_id=minty.id).cash_sale_account_id is None


@pytest.mark.django_db
def test_a_slot_takes_only_an_account_of_its_type(client, auth, minty):
    account_id = _add(client, auth, minty, code="405", name="Rent", type="expense").json()[
        "account"
    ]["id"]
    resp = post(
        client,
        auth,
        "/slots",
        {"entity_id": str(minty.id), "role": "petty_cash", "account_id": account_id},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "That account can't be used for this. Choose one from the list."
    created = _add(client, auth, minty, code="406", name="Float", type="expense", role="petty_cash")
    assert created.status_code == 400 and "type" in created.json()["fields"]


@pytest.mark.django_db
def test_petty_cash_and_the_bank_cannot_be_the_same_account(client, auth, minty):
    """As Petty Cash Settings refuses it: the float and the bank it is deposited into are two."""
    one = _add(client, auth, minty, code="090", name="Petty Cash", type="bank").json()["account"]["id"]
    two = _add(client, auth, minty, code="091", name="Bank", type="bank").json()["account"]["id"]
    sentence = ("Petty cash and the bank you deposit into can't be the same account. "
                "Choose a different one for each.")
    assert post(client, auth, "/slots", {"entity_id": str(minty.id), "role": "petty_cash", "account_id": one}
                ).status_code == 200
    same = post(client, auth, "/slots", {"entity_id": str(minty.id), "role": "bank", "account_id": one})
    assert (same.status_code, same.json()["error"]) == (400, sentence)
    assert post(client, auth, "/slots", {"entity_id": str(minty.id), "role": "bank", "account_id": two}
                ).status_code == 200
    # and the other way round
    swap = post(client, auth, "/slots", {"entity_id": str(minty.id), "role": "petty_cash", "account_id": two})
    assert (swap.status_code, swap.json()["error"]) == (400, sentence)


@pytest.mark.django_db
def test_the_slot_options_list_only_the_right_types(client, auth, minty, templates):
    _answer(client, auth, minty)
    _add(client, auth, minty, code="405", name="Rent", type="expense")
    slots = _setup(client, auth, minty)["slots"]
    assert [o["name"] for o in slots["discrepancy"]["options"]] == ["Rent"]
    assert {o["name"] for o in slots["petty_cash"]["options"]} == {"Petty Cash", "Bank Account"}


# ---- who may ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_stranger_is_refused(client, other_user, minty):
    resp = client.get(
        BASE, {"entity_id": str(minty.id)}, HTTP_AUTHORIZATION=f"Bearer {make_token(other_user.id)}"
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_a_cashier_may_look_but_not_add(client, minty):
    cashier = User.objects.create(
        id=str(uuid.uuid4()),
        email="c@example.com",
        username="c@example.com",
        password="x",
        system_role="normal",
        approved=True,
    )
    UserEntity.objects.create(user_id=cashier.id, entity_id=minty.id, role="cashier", approved=True)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {make_token(cashier.id)}"}
    assert client.get(BASE, {"entity_id": str(minty.id)}, **headers).status_code == 200
    resp = post(
        client,
        headers,
        "/accounts",
        {"entity_id": str(minty.id), "code": "405", "name": "Rent", "type": "expense"},
    )
    assert resp.status_code == 403
    assert not AccountInfo.objects.filter(entity_id=minty.id).exists()


@pytest.mark.django_db
def test_every_route_needs_an_entity_id(client, auth):
    assert client.get(BASE, **auth).status_code == 400
    assert post(client, auth, "/mode", {"mode": "minty"}).status_code == 400


# ---- the rest of the wizard ------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_minty_company_moves_past_step_4_once_it_has_answered(minty):
    modules = ["PETTY_CASH"]
    assert state.derive_current_step(minty, modules, {"connected": False}, False) == 4
    Entity.objects.filter(id=minty.id).update(uses_sales_cash=True)
    minty.refresh_from_db()
    # Petty Cash's own step (5) until its opening balance is saved (2026-11-11).
    assert state.derive_current_step(minty, modules, {"connected": False}, False) == 5
    assert state.derive_current_step(minty, modules, {"connected": False}, True) == 3
    assert state.derive_current_step(minty, ["PAYMENT_REQUEST"], {"connected": False}, False) == 8


@pytest.mark.django_db
def test_a_xero_company_still_waits_for_xero(client, auth, entity):
    post(client, auth, "/mode", {"entity_id": str(entity.id), "mode": "xero"})
    entity.refresh_from_db()
    entity.uses_sales_cash = True
    assert state.derive_current_step(entity, ["PETTY_CASH"], {"connected": False}, False) == 4


@pytest.mark.django_db
def test_bill_codes_come_from_a_minty_companys_own_expense_accounts(client, auth, minty):
    _add(client, auth, minty, code="405", name="Rent", type="expense")
    resp = client.get("/api/onboarding/bill-codes", {"entity_id": str(minty.id)}, **auth)
    assert resp.status_code == 200
    assert resp.json()["connected"] is False
    assert resp.json()["bill_codes"] == [{"code": "405", "name": "Rent"}]
    saved = client.post(
        "/api/onboarding/bill-codes",
        {"entity_id": str(minty.id), "selected_codes": ["405"], "default_code": "405"},
        content_type="application/json",
        **auth,
    )
    assert saved.status_code == 200


@pytest.mark.django_db
def test_a_xero_company_without_xero_still_cannot_load_bill_codes(client, auth, entity):
    resp = client.get("/api/onboarding/bill-codes", {"entity_id": str(entity.id)}, **auth)
    assert resp.status_code == 409


@pytest.mark.django_db
def test_minty_on_its_own_until_its_accounts_are_matched(minty):
    """An org recorded mid-matching (Settings Phase 2, 2026-11-11) leaves the company Minty's; Finish
    sets setup_mode 'xero'."""
    assert accounting.minty_on_its_own(minty)
    minty.xero_org_id = "ORG-1"
    assert accounting.minty_on_its_own(minty)
    minty.setup_mode = "xero"
    assert not accounting.minty_on_its_own(minty)


@requires_postgres
@pytest.mark.django_db
def test_a_minty_company_finalizes_without_xero(client, auth, minty, enable_module, monkeypatch):
    """Petty Cash and Payment Request on, no Xero, one daily-closing account missing: it goes live
    (as 'disconnected', the only live value before CONTRACT) and reads not_connected."""
    from core import subscription_client

    enable_module(minty, "PETTY_CASH")
    enable_module(minty, "PAYMENT_REQUEST")
    monkeypatch.setattr(subscription_client, "forward", lambda *a, **k: ({"trial_end": None}, 200))
    unanswered = client.post(
        "/api/onboarding/finalize",
        {"entity_id": str(minty.id)},
        content_type="application/json",
        **auth,
    )
    assert unanswered.status_code == 409
    assert unanswered.json()["missing"] == [
        {"step": 4, "message": "Answer the questions about your business."}
    ]

    _answer(client, auth, minty, business=None, cash=True)
    _add(client, auth, minty, code="405", name="Rent", type="expense")
    resp = client.post(
        "/api/onboarding/finalize",
        {"entity_id": str(minty.id)},
        content_type="application/json",
        **auth,
    )
    assert resp.status_code == 200, resp.content
    minty.refresh_from_db()
    assert (minty.status, minty.xero_status) == ("disconnected", "not_connected")
