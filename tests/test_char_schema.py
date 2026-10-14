"""Characterisation: one company through the wizard, pinned on the rows it leaves (C9).

    /create            -> `entities` (status onboarding), the creator's `user_entity` (super_admin),
                          one `entity_function_map` row per module (disabled, created_by = the
                          person), the eleven default `entity_sale_setting` links, one
                          `entity_cash_setting` row per active denomination of the currency
    the module step    -> Petty Cash on (as the subscription would grant it)
    the accounting step-> `xero_org_id` set; the account-code step -> `entity_pettycash_settings`
                          pointing at an `account_info` row
    /sales-methods     -> the company's `entity_sale_setting` links follow the choice
    /opening-balance   -> `entity_pettycash_settings.opening_balance` / `start_date`; no `report`
    an invitation      -> `invitation` (pending, role from the entity_role enum)
    /state             -> the step the saved data justifies, at every point

`/modules`, `/invite` and `/finalize` proxy to Flask (their effects are Minty's and are pinned
there in `test_char_entities.py`); here the rows they produce are written the way Flask
writes them. Written 2026-09-17 on the C9 models.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from core import xero_tokens
from onboarding.services import steps as step_defs
from shared_models.models import (
    AccountInfo,
    Entity,
    EntityCashSetting,
    EntityFunctionMap,
    EntityPettycashSettings,
    EntitySaleSetting,
    Invitation,
    Report,
    UserEntity,
)
from tests.conftest import requires_postgres

# Walks /state, which reads the entity_setup_summary VIEW: Postgres pass only.
pytestmark = [pytest.mark.django_db, requires_postgres]

CREATE = "/api/onboarding/create"
STATE = "/api/onboarding/state"
SALES = "/api/onboarding/sales-methods"
OPENING = "/api/onboarding/opening-balance"


def post_json(client, auth, url, body):
    return client.post(url, body, content_type="application/json", **auth)


def state_of(client, auth, entity_id):
    resp = client.get(STATE, {"entity_id": entity_id}, **auth)
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_the_wizard_leaves_the_companys_rows_on_the_schema(
    client, auth, user, countries, currencies, cash_catalogue, modules, enable_module, monkeypatch
):
    # ---- step 1: the company ---------------------------------------------------------------
    resp = post_json(client, auth, CREATE, {"entity_name": "Wizard Walk Ltd", "country_id": "HK",
                                            "currency_id": str(currencies["HKD"].id),
                                            "contact_phone": "+852 1234 5678", "business_email": "hello@walk.test"})
    assert resp.status_code == 201, resp.content
    entity_id = resp.json()["entity_id"]

    entity = Entity.objects.get(id=entity_id)
    assert entity.status == "onboarding" and entity.country_code == "HK"
    membership = UserEntity.objects.get(entity_id=entity_id, user_id=user.id)
    assert membership.role == "admin" and membership.approved is True  # the creator is the company admin
    by_function = {str(fn.id): code for code, fn in modules.items()}
    grants = {by_function[str(m.entity_function_id)]: m for m in EntityFunctionMap.objects.filter(entity_id=entity_id)}
    assert set(grants) == set(modules) and not any(m.is_enabled for m in grants.values())
    assert all(str(m.created_by) == str(user.id) for m in grants.values())
    defaults = EntitySaleSetting.objects.filter(entity_id=entity_id)
    # Cash only: electronic/delivery start empty until the wizard's Auto Fill (2026-10-02).
    assert [s.sale.value_name for s in defaults.select_related("sale")] == ["cash_sales"]
    assert all(s.is_active for s in defaults)
    cash = {str(c.cash_id): c for c in EntityCashSetting.objects.filter(entity_id=entity_id)}
    assert set(cash) == {str(c.id) for c in cash_catalogue["HKD"]}  # active HKD only
    assert all(c.is_active for c in cash.values())
    assert state_of(client, auth, entity_id)["current_step"] == step_defs.STEP_MODULE

    # ---- step 2: the module (Flask grants it; the row is what Flask writes) ------------------
    EntityFunctionMap.objects.filter(entity_id=entity_id, entity_function_id=modules["PETTY_CASH"].id).delete()
    enable_module(entity, "PETTY_CASH")
    assert state_of(client, auth, entity_id)["modules"] == ["PETTY_CASH"]
    assert state_of(client, auth, entity_id)["current_step"] == step_defs.STEP_ACCOUNTING

    # ---- step 4: accounting (Xero connected; the reconcile cannot verify - left alone) ------
    monkeypatch.setattr(xero_tokens, "connected_tenant_ids", lambda _eid: None)
    Entity.objects.filter(id=entity_id).update(xero_org_id=str(uuid.uuid4()), status="onboarding")
    assert state_of(client, auth, entity_id)["current_step"] == step_defs.STEP_SALES

    # ---- step 5: sales methods -------------------------------------------------------------
    resp = post_json(client, auth, SALES, {"entity_id": entity_id, "electronic": ["Visa", "Octopus"], "delivery": ["Foodpanda"]})
    assert resp.status_code == 200, resp.content
    active = {s.sale.sale_name for s in EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True).select_related("sale")}
    assert {"Visa", "Octopus", "Foodpanda", "Cash"} <= active
    body = state_of(client, auth, entity_id)
    assert "Octopus" in body["sales_methods"]["electronic"]  # the POST body shape: names per type

    # ---- step 6: account codes (Flask writes the settings; the row points at account_info) ---
    account = AccountInfo.objects.create(
        id=uuid.uuid4(), entity_id=entity_id, type="CURRENT", name="Petty Cash",
        xero_account_id=str(uuid.uuid4()), xero_code="090", status="ACTIVE",
    )
    EntityPettycashSettings.objects.create(entity_id=entity_id, pettycash_account_id=account.id)
    assert state_of(client, auth, entity_id)["current_step"] == step_defs.STEP_INVITE  # account codes done, no bill module -> invite

    # ---- opening balance: on the settings row Step 6 made; no report ----------------------
    resp = post_json(client, auth, OPENING, {"entity_id": entity_id, "opening_date": date.today().isoformat(), "cash_addition": "1500.50"})
    assert resp.status_code == 200, resp.content
    settings_row = EntityPettycashSettings.objects.get(entity_id=entity_id)
    assert settings_row.opening_balance == Decimal("1500.50") and settings_row.start_date == date.today()
    assert settings_row.pettycash_account_id == account.id  # Step 6's mapping survives
    assert not Report.objects.filter(entity_id=entity_id).exists()
    resp = post_json(client, auth, OPENING, {"entity_id": entity_id, "opening_date": date.today().isoformat(), "cash_addition": "2000"})
    assert resp.status_code == 200
    assert state_of(client, auth, entity_id)["opening_balance"]["opening_balance"] == 2000.0

    # ---- an invitation (Flask writes it; the row on the schema) ----------------------------
    Invitation.objects.create(
        id=uuid.uuid4(), entity_id=entity_id, email="new@walk.test", role="cashier",
        token=uuid.uuid4().hex, status="pending", invited_by=user.id,
    )
    pending = state_of(client, auth, entity_id)["invites"]
    assert [(i["email"], i["role"]) for i in pending] == [("new@walk.test", "cashier")]

    # ---- a finished company reports the terminal step, whatever else is saved --------------
    Entity.objects.filter(id=entity_id).update(status="connected")
    assert state_of(client, auth, entity_id)["current_step"] == step_defs.STEP_ALL_SET
