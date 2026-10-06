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
