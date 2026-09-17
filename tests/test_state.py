"""Group B -- wizard state.

This is the endpoint a cold resume depends on, so the tests are weighted toward the two
things that actually break it: which step the saved data justifies, and the access checks.

The Xero reconcile tests are the ones worth reading. Getting "could not verify" confused
with "disconnected" clears a live Xero connection over a network blip, and no status code
would reveal it.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from core import xero_tokens
from onboarding.services import state as state_service
from onboarding.services import steps as step_defs
from shared_models.models import (EntityPettycashSettings, EntitySaleSetting, SaleInfo,
                                  Invitation, Report, UserEntity)
from tests.conftest import make_token

STATE = "/api/onboarding/state"
SAVED_STEP = "/api/onboarding/saved-step"


@pytest.fixture
def xero_unverifiable(monkeypatch):
    """Xero is CONNECTED as far as the row says, and the reconcile cannot verify it.

    For tests about step derivation that need `xero_org_id` set and do not care about the
    reconcile. Without a stub the reconcile really POSTs to the token service -- see the
    network guard in conftest.
    """
    monkeypatch.setattr(xero_tokens, "connected_tenant_ids", lambda _eid: None)


def state_of(client, auth, entity):
    resp = client.get(STATE, {"entity_id": entity.id}, **auth)
    assert resp.status_code == 200, resp.content
    return resp.json()


# ---------------------------------------------------------------------------
# Access. The ORDER of these checks is part of the contract.
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_missing_entity_id_is_400(client, auth):
    resp = client.get(STATE, **auth)
    assert resp.status_code == 400
    assert resp.json() == {"error": "entity_id is required"}


@pytest.mark.django_db
def test_non_member_is_403(client, other_user, entity):
    token = make_token(other_user.id)
    resp = client.get(
        STATE, {"entity_id": entity.id}, HTTP_AUTHORIZATION=f"Bearer {token}"
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have access to this entity"}


@pytest.mark.django_db
def test_unknown_entity_is_404_only_for_a_member(client, auth, user):
    """404 is reachable only once membership is established.

    Membership is checked BEFORE existence, deliberately. If existence came first, the
    403-vs-404 split would let anyone enumerate which entity ids are real by probing. So a
    stranger gets 403 for a missing entity too -- which looks wrong and is not.
    """
    ghost = str(uuid.uuid4())
    UserEntity.objects.create(
        user_id=user.id, entity_id=ghost, role="super_admin", approved=True
    )
    resp = client.get(STATE, {"entity_id": ghost}, **auth)
    assert resp.status_code == 404
    assert resp.json() == {"error": "Entity not found"}


@pytest.mark.django_db
def test_a_stranger_cannot_tell_missing_from_forbidden(client, other_user, entity):
    """The probe-resistance property, asserted directly."""
    token = make_token(other_user.id)
    real = client.get(
        STATE, {"entity_id": entity.id}, HTTP_AUTHORIZATION=f"Bearer {token}"
    )
    fake = client.get(
        STATE, {"entity_id": str(uuid.uuid4())}, HTTP_AUTHORIZATION=f"Bearer {token}"
    )
    assert real.status_code == fake.status_code == 403
    assert real.json() == fake.json()


# ---------------------------------------------------------------------------
# Module resolution -- fail closed
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_no_map_row_means_no_module(client, auth, entity, modules):
    """FAIL CLOSED, and this exact case was once a real bug.

    Flask's version fell back to the catalog's ``is_active`` where no map row existed.
    ``is_active`` defaults True, so a brand-new entity answered "both modules enabled",
    ``current_step`` skipped STEP_MODULE, and the user landed past the very selection they
    had not made yet.

    The catalog says whether a module is OFFERED, never who may use it.
    """
    body = state_of(client, auth, entity)
    assert body["modules"] == []
    assert body["current_step"] == step_defs.STEP_MODULE


@pytest.mark.django_db
def test_a_disabled_map_row_is_not_enabled(client, auth, entity, enable_module):
    enable_module(entity, "PETTY_CASH", on=False)
    assert state_of(client, auth, entity)["modules"] == []


@pytest.mark.django_db
def test_modules_come_back_in_canonical_order(client, auth, entity, enable_module):
    """BILL is enabled first, but PETTY_CASH must still be listed first.

    Order is MODULE_CODES order, not insertion order -- the wizard renders the list
    directly.
    """
    enable_module(entity, "PAYMENT_REQUEST")
    enable_module(entity, "PETTY_CASH")
    assert state_of(client, auth, entity)["modules"] == ["PETTY_CASH", "PAYMENT_REQUEST"]


# ---------------------------------------------------------------------------
# Step derivation
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_no_modules_lands_on_the_module_step(client, auth, entity, modules):
    assert state_of(client, auth, entity)["current_step"] == step_defs.STEP_MODULE


@pytest.mark.django_db
def test_modules_but_no_xero_lands_on_accounting(client, auth, entity, enable_module):
    enable_module(entity, "PETTY_CASH")
    assert state_of(client, auth, entity)["current_step"] == step_defs.STEP_ACCOUNTING


@pytest.mark.django_db
def test_petty_cash_without_account_codes_lands_on_sales(
    client, auth, entity, enable_module, xero_unverifiable
):
    enable_module(entity, "PETTY_CASH")
    entity.xero_org_id = str(uuid.uuid4())
    entity.save()
    assert state_of(client, auth, entity)["current_step"] == step_defs.STEP_SALES


@pytest.mark.django_db
def test_petty_cash_with_account_codes_and_no_bill_lands_on_invite(
    client, auth, entity, enable_module, xero_unverifiable
):
    enable_module(entity, "PETTY_CASH")
    entity.xero_org_id = str(uuid.uuid4())
    entity.save()
    EntityPettycashSettings.objects.create(
        entity_id=entity.id, pettycash_account_id=str(uuid.uuid4())
    )
    assert state_of(client, auth, entity)["current_step"] == step_defs.STEP_INVITE


@pytest.mark.django_db
def test_bill_module_lands_on_bills(client, auth, entity, enable_module, xero_unverifiable):
    enable_module(entity, "PAYMENT_REQUEST")
    entity.xero_org_id = str(uuid.uuid4())
    entity.save()
    assert state_of(client, auth, entity)["current_step"] == step_defs.STEP_BILLS


@pytest.mark.django_db
def test_a_finalized_entity_reports_the_terminal_step(client, auth, entity, modules):
    """status != 'onboarding' short-circuits everything else.

    Asserted with no modules enabled, so it is clear the status wins rather than the data
    happening to justify step 9.
    """
    entity.status = "connected"  # the entity_status word for a finished company (C2)
    entity.save()
    body = state_of(client, auth, entity)
    assert body["current_step"] == step_defs.STEP_ALL_SET
    assert body["status"] == "connected"


@pytest.mark.django_db
def test_an_empty_pettycash_settings_row_does_not_count_as_done(
    client, auth, entity, enable_module, xero_unverifiable
):
    """The row exists before it is filled in, so presence is not completion."""
    enable_module(entity, "PETTY_CASH")
    entity.xero_org_id = str(uuid.uuid4())
    entity.save()
    EntityPettycashSettings.objects.create(entity_id=entity.id, pettycash_account_id=None)
    assert state_of(client, auth, entity)["current_step"] == step_defs.STEP_SALES


# ---------------------------------------------------------------------------
# saved_step vs current_step -- two different numbers
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_saved_step_is_returned_verbatim_not_remapped(client, auth, entity, modules):
    """saved_step is the FRONTEND id; current_step is the BACKEND's derivation.

    Both are 1-9 and they mean different things. Here the entity has no modules, so
    current_step is 2 -- while saved_step stays at the 7 the wizard sent. If these were
    ever conflated, one would silently overwrite the other.
    """
    entity.onboarding_saved_step = 7
    entity.save()
    body = state_of(client, auth, entity)
    assert body["saved_step"] == 7
    assert body["current_step"] == step_defs.STEP_MODULE


@pytest.mark.django_db
def test_saved_step_is_null_when_never_set(client, auth, entity, modules):
    assert state_of(client, auth, entity)["saved_step"] is None


@pytest.mark.django_db
@pytest.mark.parametrize("step", [1, 5, 9])
def test_saved_step_round_trips(client, auth, entity, modules, step):
    resp = client.post(
        SAVED_STEP,
        {"entity_id": entity.id, "saved_step": step},
        content_type="application/json",
        **auth,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "saved_step": step}
    assert state_of(client, auth, entity)["saved_step"] == step


@pytest.mark.django_db
@pytest.mark.parametrize("bad", [0, 10, -1, "abc", None, 2.5, [], {}])
def test_saved_step_rejects_out_of_range_and_non_integers(client, auth, entity, bad):
    """Flask's exact sentence, because the wizard renders it verbatim.

    2.5 is in the list on purpose: int(2.5) succeeds and would silently store 2, which is a
    different step than the user was on.
    """
    resp = client.post(
        SAVED_STEP,
        {"entity_id": entity.id, "saved_step": bad},
        content_type="application/json",
        **auth,
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "saved_step must be an integer 1-9"}


@pytest.mark.django_db
def test_saved_step_rejects_booleans(client, auth, entity):
    """``bool`` is an ``int`` subclass, so ``int(True)`` is ``1``.

    JSON ``true`` would otherwise be stored as step 1 -- a real step, silently, from a value
    that means nothing of the kind.
    """
    resp = client.post(
        SAVED_STEP,
        {"entity_id": entity.id, "saved_step": True},
        content_type="application/json",
        **auth,
    )
    assert resp.status_code == 400
    entity.refresh_from_db()
    assert entity.onboarding_saved_step is None


@pytest.mark.django_db
def test_saved_step_accepts_a_digit_string(client, auth, entity, modules):
    """Flask accepts these, and a client sending "5" is being unremarkable, not wrong.

    Pinned so the strictness added for floats and booleans is not widened into rejecting
    this as well.
    """
    resp = client.post(
        SAVED_STEP,
        {"entity_id": entity.id, "saved_step": "5"},
        content_type="application/json",
        **auth,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "saved_step": 5}


@pytest.mark.django_db
def test_saved_step_requires_membership(client, other_user, entity):
    token = make_token(other_user.id)
    resp = client.post(
        SAVED_STEP,
        {"entity_id": entity.id, "saved_step": 3},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert resp.status_code == 403
    entity.refresh_from_db()
    assert entity.onboarding_saved_step is None


# ---------------------------------------------------------------------------
# Entity block -- presence matters, not just value
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_contact_fields_are_always_present_as_empty_strings(
    client, auth, entity, modules
):
    """The wizard's rehydration keys off PRESENCE, and an empty value is legitimate.

    So an unset phone must arrive as "" rather than being omitted or null -- otherwise the
    wizard cannot tell "the server has no value" from "the server didn't send the field".
    """
    body = state_of(client, auth, entity)["entity"]
    assert body["phone"] == ""
    assert body["email"] == ""
    assert set(body) == {"name", "country", "currency", "phone", "email"}


@pytest.mark.django_db
def test_currency_is_a_string_not_a_uuid_object(client, auth, entity, modules, currencies):
    """THE UUID TRAP, on the way out of state.

    The wizard submits this value straight back when editing Step 1. Django hands back a
    UUID object where Flask hands back a string.
    """
    entity.currency_id = currencies["HKD"].id
    entity.save()
    value = state_of(client, auth, entity)["entity"]["currency"]
    assert isinstance(value, str)
    assert uuid.UUID(value) == currencies["HKD"].id


@pytest.mark.django_db
def test_currency_is_empty_string_when_unset(client, auth, entity, modules):
    assert state_of(client, auth, entity)["entity"]["currency"] == ""


# ---------------------------------------------------------------------------
# The Xero reconcile. "Could not verify" is NOT "disconnected".
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_confirmed_revoke_clears_connection_state(
    client, auth, user, entity, modules, monkeypatch
):
    """Xero answered, and the tenant is gone. That is evidence -- clear local state."""
    org = str(uuid.uuid4())
    entity.xero_org_id = org
    entity.xero_tenant_name = "Gone Ltd"
    entity.status = "onboarding"
    entity.connected_by_user_id = user.id  # a uuid FK to user now
    entity.save()

    monkeypatch.setattr(
        xero_tokens, "connected_tenant_ids", lambda _eid: {str(uuid.uuid4())}
    )

    body = state_of(client, auth, entity)
    assert body["xero"]["connected"] is False
    entity.refresh_from_db()
    assert entity.xero_org_id is None
    assert entity.connected_by_user_id is None


@pytest.mark.django_db
def test_an_unverifiable_connection_is_left_alone(
    client, auth, entity, modules, monkeypatch
):
    """THE ASYMMETRY THIS WHOLE PATH EXISTS FOR.

    None means "could not verify" -- no connector, an unusable token, Xero unreachable, a
    non-200. It is NOT an empty set, and treating the two alike would drop a live Xero
    connection over a network blip.

    Wrongly showing connected costs one confusing screen. Wrongly showing disconnected
    costs a reconnect the user did not need.
    """
    org = str(uuid.uuid4())
    entity.xero_org_id = org
    entity.save()

    monkeypatch.setattr(xero_tokens, "connected_tenant_ids", lambda _eid: None)

    body = state_of(client, auth, entity)
    assert body["xero"]["connected"] is True
    entity.refresh_from_db()
    assert entity.xero_org_id == org


@pytest.mark.django_db
def test_a_still_connected_org_is_left_alone(client, auth, entity, modules, monkeypatch):
    org = str(uuid.uuid4())
    entity.xero_org_id = org
    entity.save()
    monkeypatch.setattr(xero_tokens, "connected_tenant_ids", lambda _eid: {org})
    assert state_of(client, auth, entity)["xero"]["connected"] is True
    entity.refresh_from_db()
    assert entity.xero_org_id == org


@pytest.mark.django_db
def test_no_xero_org_does_not_call_xero_at_all(client, auth, entity, modules, monkeypatch):
    """An unconnected entity must not cost a token request on every resume."""
    called = []
    monkeypatch.setattr(
        xero_tokens, "connected_tenant_ids", lambda eid: called.append(eid)
    )
    body = state_of(client, auth, entity)
    assert body["xero"] == {"connected": False, "org": ""}
    assert called == []


# ---------------------------------------------------------------------------
# Sales methods and the opening draft
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_sales_methods_group_by_type_and_respect_display_order(
    client, auth, entity, modules
):
    for i, (typ, name) in enumerate(
        [("electronic", "Visa"), ("delivery", "Foodpanda"), ("electronic", "Octopus")]
    ):
        catalog = SaleInfo.objects.create(id=uuid.uuid4(), sale_name=name, type=typ, enabled=True)
        EntitySaleSetting.objects.create(
            entity_id=entity.id, sale=catalog, is_active=True, display_order=10 - i,
        )
    methods = state_of(client, auth, entity)["sales_methods"]
    # display_order ascending: Octopus (8), Foodpanda (9), Visa (10)
    assert methods["electronic"] == ["Octopus", "Visa"]
    assert methods["delivery"] == ["Foodpanda"]


@pytest.mark.django_db
def test_disabled_and_other_typed_sales_methods_are_excluded(client, auth, entity, modules):
    off = SaleInfo.objects.create(id=uuid.uuid4(), sale_name="Switched off", type="electronic", enabled=True)
    cash = SaleInfo.objects.create(id=uuid.uuid4(), sale_name="Cash", type="other", value_name="cash_sales", enabled=True)
    EntitySaleSetting.objects.create(entity_id=entity.id, sale=off, is_active=False, display_order=1)
    EntitySaleSetting.objects.create(entity_id=entity.id, sale=cash, is_active=True, display_order=1)
    assert state_of(client, auth, entity)["sales_methods"] == {
        "electronic": [],
        "delivery": [],
    }


@pytest.mark.django_db
def test_opening_balance_is_null_with_no_draft(client, auth, entity, modules):
    assert state_of(client, auth, entity)["opening_balance"] is None


@pytest.mark.django_db
def test_opening_balance_reads_the_earliest_draft(client, auth, entity, modules):
    """Earliest by transaction_date -- the opening one, not the most recent."""
    for offset, amount in [(5, 999.0), (0, 500.0)]:
        Report.objects.create(
            id=str(uuid.uuid4()), entity_id=entity.id, status="draft",
            transaction_date=date.today() + timedelta(days=offset),
            opening_balance=amount, cash_addition=0.0,
        )
    opening = state_of(client, auth, entity)["opening_balance"]
    assert opening["opening_balance"] == 500.0
    assert opening["cash_addition"] == 0.0


@pytest.mark.django_db
def test_a_posted_report_is_not_the_opening_draft(client, auth, entity, modules):
    Report.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, status="submitted",
        transaction_date=date.today(), opening_balance=123.0, cash_addition=0.0,
    )
    assert state_of(client, auth, entity)["opening_balance"] is None


# ---------------------------------------------------------------------------
# Invites -- best effort, never fatal
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_pending_invites_are_listed_for_a_super_admin(client, auth, entity, modules):
    Invitation.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, email="new@example.com",
        role="cashier", token=str(uuid.uuid4()), status="pending",
        first_name="New", last_name="Hire",
        created_at=datetime.now(timezone.utc),
    )
    invites = state_of(client, auth, entity)["invites"]
    assert [i["email"] for i in invites] == ["new@example.com"]
    assert invites[0]["first_name"] == "New"


@pytest.mark.django_db
def test_non_pending_invites_are_excluded(client, auth, entity, modules):
    for status in ("accepted", "revoked"):
        Invitation.objects.create(
            id=str(uuid.uuid4()), entity_id=entity.id, email=f"{status}@example.com",
            role="cashier", token=str(uuid.uuid4()), status=status,
            created_at=datetime.now(timezone.utc),
        )
    assert state_of(client, auth, entity)["invites"] == []


@pytest.mark.django_db
def test_a_cashier_gets_an_empty_invite_list_not_a_403(client, user, entity, modules):
    """BEST EFFORT ON PURPOSE.

    Listing invites needs USER_VIEW_ALL (shop_manager and above). A cashier invited into a
    half-finished onboarding legitimately lacks it -- and their resume must still work, so
    the permission gap yields an empty list rather than failing the whole state read.

    Note what is being protected here: not the invite list, but the resume.
    """
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    Invitation.objects.create(
        id=str(uuid.uuid4()), entity_id=entity.id, email="hidden@example.com",
        role="cashier", token=str(uuid.uuid4()), status="pending",
        created_at=datetime.now(timezone.utc),
    )
    resp = client.get(
        STATE,
        {"entity_id": entity.id},
        HTTP_AUTHORIZATION=f"Bearer {make_token(user.id)}",
    )
    assert resp.status_code == 200
    assert resp.json()["invites"] == []


# ---------------------------------------------------------------------------
# The response shape is EXACTLY Flask's
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_response_carries_flasks_keys_and_no_others(client, auth, entity, modules):
    """Byte-for-byte key parity, asserted in both directions.

    This briefly carried an extra ``steps`` key -- the wizard step table -- on the theory
    that the frontend duplicated an ordering the backend owned. It does not: the frontend
    derives its landing step from a DIFFERENT ordering on purpose, and documents the bug
    that trusting this one caused. The key was withdrawn rather than left unused, so there
    is now no divergence on this endpoint at all.

    Asserting equality rather than a subset is what keeps it that way: adding a key here
    fails this test, which is the prompt to ask whether the frontend actually wants it.
    """
    flask_keys = {
        "entity_id", "status", "current_step", "max_reached", "saved_step", "entity",
        "modules", "xero", "sales_methods", "opening_balance", "invites",
    }
    assert set(state_of(client, auth, entity)) == flask_keys


@pytest.mark.django_db
def test_derive_current_step_is_reachable_without_http(entity):
    """The derivation is a pure function of its four inputs, and worth being able to
    call directly -- the parity sweep exercises it against real rows, but a regression
    here should be diagnosable without a database."""
    assert (
        state_service.derive_current_step(entity, [], {"connected": True}, True)
        == step_defs.STEP_MODULE
    )
