"""POST /api/onboarding/finalize -- the status flip here, the trial start on the subscription API.

Flask's finalize started the trials best-effort and answered success whatever happened, so
the All Set screen could announce a trial that did not exist. Here a failed trial start FAILS
finalize with the subscription API's own status and sentence, the company stays live, and a
retry (Try again) is safe because both halves are idempotent.

Stubbed at the ``requests`` boundary, like test_minty_client.py, so the real transport runs.
"""

import json

import pytest
import requests
from django.conf import settings

from shared_models.models import Entity
from tests.test_minty_client import FakeResponse, Recorder

pytestmark = pytest.mark.django_db

TRIAL_END = "2026-11-05T00:00:00+00:00"


def _finalize(client, auth, entity_id):
    return client.post(
        "/api/onboarding/finalize",
        data=json.dumps({"entity_id": str(entity_id)}),
        content_type="application/json",
        **auth,
    )


def test_finalize_flips_the_company_and_states_the_trial_end(client, auth, entity, monkeypatch):
    rec = Recorder(FakeResponse(200, {"trial_end": TRIAL_END}))
    monkeypatch.setattr(requests, "request", rec)

    resp = _finalize(client, auth, entity.id)

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "trial_end": TRIAL_END}
    entity.refresh_from_db()
    assert entity.status == "disconnected"  # no Xero org linked
    [(method, url, kwargs)] = rec.calls
    assert method == "POST"
    assert url == f"{settings.SUBSCRIPTION_API_URL}/api/onboarding/trials/start"
    assert kwargs["json"] == {"entity_id": str(entity.id)}
    assert kwargs["headers"]["Authorization"] == auth["HTTP_AUTHORIZATION"]


def test_a_company_with_xero_goes_live_connected(client, auth, entity, monkeypatch):
    Entity.objects.filter(id=entity.id).update(xero_org_id="org-1")
    monkeypatch.setattr(requests, "request", Recorder(FakeResponse(200, {"trial_end": None})))

    resp = _finalize(client, auth, entity.id)

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "trial_end": None}
    entity.refresh_from_db()
    assert entity.status == "connected"


def test_a_failed_trial_start_fails_finalize_and_keeps_the_company_live(
    client, auth, entity, monkeypatch
):
    sentence = "This trial could not be started. Mind trying again?"
    monkeypatch.setattr(requests, "request", Recorder(FakeResponse(502, {"error": sentence})))

    resp = _finalize(client, auth, entity.id)

    assert resp.status_code == 502
    assert resp.json() == {"error": sentence}
    entity.refresh_from_db()
    assert entity.status == "disconnected"


def test_try_again_reaches_the_trial_start_on_a_live_company(client, auth, entity, monkeypatch):
    """The retry finds the company already live and still asks for the trials."""
    monkeypatch.setattr(requests, "request", Recorder(FakeResponse(502, {"error": "x"})))
    assert _finalize(client, auth, entity.id).status_code == 502

    rec = Recorder(FakeResponse(200, {"trial_end": TRIAL_END}))
    monkeypatch.setattr(requests, "request", rec)
    resp = _finalize(client, auth, entity.id)

    assert resp.status_code == 200
    assert resp.json()["trial_end"] == TRIAL_END
    assert len(rec.calls) == 1


def test_an_unreachable_subscription_api_fails_finalize(client, auth, entity, monkeypatch):
    monkeypatch.setattr(requests, "request", Recorder(raises=requests.ConnectionError()))
    resp = _finalize(client, auth, entity.id)
    assert resp.status_code == 502


def test_a_stranger_cannot_finalize(client, other_user, entity, monkeypatch):
    from tests.conftest import make_token

    rec = Recorder(FakeResponse(200, {"trial_end": None}))
    monkeypatch.setattr(requests, "request", rec)
    resp = _finalize(
        client, {"HTTP_AUTHORIZATION": f"Bearer {make_token(other_user.id)}"}, entity.id
    )

    assert resp.status_code == 403
    entity.refresh_from_db()
    assert entity.status == "onboarding"
    assert rec.calls == []


def test_finalize_needs_an_entity_id(client, auth):
    resp = client.post("/api/onboarding/finalize", data="{}", content_type="application/json", **auth)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Setup must be complete before a company goes live (2026-10-13)
#
# Petty Cash needs Xero and the six accounts + three contacts publishing uses; Payment Request
# needs Xero and at least one active bill account code. Read from entity_setup_summary, so these
# run in the Postgres pass.
# ---------------------------------------------------------------------------
import uuid  # noqa: E402

from django.db import connection  # noqa: E402

from shared_models.models import AccountInfo, EntityPettycashSettings  # noqa: E402
from tests.conftest import requires_postgres  # noqa: E402


def _xero(entity):
    Entity.objects.filter(id=entity.id).update(xero_org_id="org-1")


def _map_petty_cash(entity, *, accounts=6, contacts=3):
    acc = [AccountInfo.objects.create(id=uuid.uuid4(), entity_id=entity.id, type="BANK", name=f"A{i}")
           for i in range(accounts)]
    con = []
    with connection.cursor() as cur:
        for i in range(contacts):
            cid = uuid.uuid4()
            cur.execute("INSERT INTO xero_contact_sync (id, entity_id, xero_contact_id, name) "
                        "VALUES (%s, %s, %s, %s)", [str(cid), str(entity.id), f"c{i}", f"C{i}"])
            con.append(cid)
    cols = ["pettycash_account_id", "bank_account_id", "cash_sale_account_id",
            "discrepancy_bank_account_id", "discrepancy_account_id", "director_account_id"]
    ccols = ["cash_sale_contact_id", "director_contact_id", "discrepancy_contact_id"]
    EntityPettycashSettings.objects.create(
        entity_id=entity.id,
        **{c: a.id for c, a in zip(cols, acc)},
        **{c: x for c, x in zip(ccols, con)},
    )


def _bill_code(entity, code="400", *, active=True, default=False):
    with connection.cursor() as cur:
        cur.execute("INSERT INTO entity_bill_account_xero (entity_id, account_code, is_active, is_default) "
                    "VALUES (%s, %s, %s, %s)", [str(entity.id), code, active, default])


def _trials_ok(monkeypatch):
    rec = Recorder(FakeResponse(200, {"trial_end": TRIAL_END}))
    monkeypatch.setattr(requests, "request", rec)
    return rec


@requires_postgres
def test_petty_cash_without_xero_is_not_finalized(client, auth, entity, enable_module, monkeypatch):
    enable_module(entity, "PETTY_CASH")
    rec = _trials_ok(monkeypatch)

    resp = _finalize(client, auth, entity.id)

    assert resp.status_code == 409
    assert "Xero" in resp.json()["error"]
    assert rec.calls == []  # no trial started
    entity.refresh_from_db()
    assert entity.status == "onboarding"


@requires_postgres
def test_petty_cash_with_a_partial_mapping_names_what_is_missing(
    client, auth, entity, enable_module, monkeypatch
):
    enable_module(entity, "PETTY_CASH")
    _xero(entity)
    _map_petty_cash(entity, accounts=6, contacts=2)
    _trials_ok(monkeypatch)

    resp = _finalize(client, auth, entity.id)

    assert resp.status_code == 409
    assert "contact" in resp.json()["error"].lower()


@requires_postgres
def test_petty_cash_fully_set_up_goes_live(client, auth, entity, enable_module, monkeypatch):
    enable_module(entity, "PETTY_CASH")
    _xero(entity)
    _map_petty_cash(entity)
    _trials_ok(monkeypatch)

    assert _finalize(client, auth, entity.id).status_code == 200


@requires_postgres
def test_payment_request_needs_xero_and_an_active_bill_code(
    client, auth, entity, enable_module, monkeypatch
):
    enable_module(entity, "PAYMENT_REQUEST")
    _trials_ok(monkeypatch)
    assert _finalize(client, auth, entity.id).status_code == 409  # no Xero

    _xero(entity)
    _bill_code(entity, "400", active=False)
    resp = _finalize(client, auth, entity.id)
    assert resp.status_code == 409 and "account code" in resp.json()["error"].lower()

    _bill_code(entity, "410", active=True)
    assert _finalize(client, auth, entity.id).status_code == 200


@requires_postgres
def test_a_default_bill_code_must_be_active(client, auth, entity, enable_module, monkeypatch):
    enable_module(entity, "PAYMENT_REQUEST")
    _xero(entity)
    _bill_code(entity, "400", active=True)
    _bill_code(entity, "410", active=False, default=True)
    _trials_ok(monkeypatch)

    assert _finalize(client, auth, entity.id).status_code == 409


@requires_postgres
def test_both_modules_need_both_setups(client, auth, entity, enable_module, monkeypatch):
    enable_module(entity, "PETTY_CASH")
    enable_module(entity, "PAYMENT_REQUEST")
    _xero(entity)
    _map_petty_cash(entity)
    _trials_ok(monkeypatch)
    assert _finalize(client, auth, entity.id).status_code == 409  # no bill code yet

    _bill_code(entity, "400")
    assert _finalize(client, auth, entity.id).status_code == 200
