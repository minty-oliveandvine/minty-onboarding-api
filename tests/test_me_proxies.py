"""``/me/profile`` and ``/me/subscriptions`` -- the sidebar's My Profile sheet, proxied.

The wizard draws ``@minty/shared``'s sidebar, whose My Profile sheet reads and saves the
person's profile (Flask's ``/api/me/profile``) and lists their subscriptions (the
subscription API's ``/api/me/subscriptions``). Neither of those admits the onboarding origin
through CORS, so the wizard reaches them through here, with its own bearer forwarded.

Stubbed at the ``requests`` boundary, like test_minty_client.py, so the real transport runs:
the method (PATCH included), the forwarded header, the query and the body are all checked as
``requests`` would receive them.
"""

import json

import pytest
import requests
from django.conf import settings

from tests.test_minty_client import FakeResponse, Recorder

pytestmark = pytest.mark.django_db

PROFILE = "/api/onboarding/me/profile"
SUBSCRIPTIONS = "/api/onboarding/me/subscriptions"
CONTACT = "/api/onboarding/me/contact"


def _install(monkeypatch, status=200, body=None):
    rec = Recorder(FakeResponse(status, {} if body is None else body))
    monkeypatch.setattr(requests, "request", rec)
    return rec


# --- GET /me/profile -> Flask GET /api/me/profile ---------------------------------------


def test_profile_read_forwards_the_token_and_the_entity(client, auth, monkeypatch):
    rec = _install(monkeypatch, body={"first_name": "Ada"})

    resp = client.get(PROFILE, {"entity": "e-1"}, **auth)

    assert resp.status_code == 200
    assert resp.json() == {"first_name": "Ada"}
    [(method, url, kwargs)] = rec.calls
    assert method == "GET"
    assert url == f"{settings.PETTY_CASH_URL}/api/me/profile"
    assert kwargs["params"] == {"entity": "e-1"}
    assert kwargs["json"] is None
    assert kwargs["headers"]["Authorization"] == auth["HTTP_AUTHORIZATION"]


def test_profile_read_without_an_entity_sends_no_entity(client, auth, monkeypatch):
    """Opened outside a company, the profile describes the person alone -- so no
    ``entity=`` reaches Flask, not even an empty one."""
    rec = _install(monkeypatch, body={"first_name": "Ada"})

    assert client.get(PROFILE, **auth).status_code == 200
    [(_, _, kwargs)] = rec.calls
    assert not kwargs["params"]


def test_profile_read_relays_flasks_refusal(client, auth, monkeypatch):
    _install(monkeypatch, 403, {"error": "You don't have access to that company."})

    resp = client.get(PROFILE, {"entity": "e-2"}, **auth)

    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have access to that company."}


# --- PATCH /me/profile -> Flask PATCH /api/me/profile -----------------------------------


def test_profile_save_forwards_the_body_unchanged(client, auth, monkeypatch):
    rec = _install(monkeypatch, body={"first_name": "Grace"})
    body = {"first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com"}

    resp = client.patch(
        f"{PROFILE}?entity=e-1",
        data=json.dumps(body),
        content_type="application/json",
        **auth,
    )

    assert resp.status_code == 200
    assert resp.json() == {"first_name": "Grace"}
    [(method, url, kwargs)] = rec.calls
    assert method == "PATCH"
    assert url == f"{settings.PETTY_CASH_URL}/api/me/profile"
    assert kwargs["json"] == body
    assert kwargs["params"] == {"entity": "e-1"}
    assert kwargs["headers"]["Authorization"] == auth["HTTP_AUTHORIZATION"]


@pytest.mark.parametrize("status", [400, 422])
def test_profile_save_relays_flasks_refusal(client, auth, monkeypatch, status):
    """Flask validates the fields; its sentence reaches the sheet as sent."""
    refusal = {"error": "That email address doesn't look right."}
    _install(monkeypatch, status, refusal)

    resp = client.patch(
        PROFILE,
        data=json.dumps({"email": "nope"}),
        content_type="application/json",
        **auth,
    )

    assert resp.status_code == status
    assert resp.json() == refusal


# --- GET /me/subscriptions -> subscription API GET /api/me/subscriptions ----------------


def test_subscriptions_forward_the_token_and_the_page(client, auth, monkeypatch):
    rec = _install(monkeypatch, body={"items": [], "total": 0})

    resp = client.get(SUBSCRIPTIONS, {"page": "2", "per_page": "5"}, **auth)

    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}
    [(method, url, kwargs)] = rec.calls
    assert method == "GET"
    assert url == f"{settings.SUBSCRIPTION_API_URL}/api/me/subscriptions"
    assert kwargs["params"] == {"page": "2", "per_page": "5"}
    assert kwargs["headers"]["Authorization"] == auth["HTTP_AUTHORIZATION"]


def test_subscriptions_without_paging_send_none(client, auth, monkeypatch):
    rec = _install(monkeypatch, body={"items": []})

    assert client.get(SUBSCRIPTIONS, **auth).status_code == 200
    [(_, _, kwargs)] = rec.calls
    assert not kwargs["params"]


def test_subscriptions_relay_the_upstream_status(client, auth, monkeypatch):
    _install(monkeypatch, 400, {"error": "Choose a page from 1 up."})

    resp = client.get(SUBSCRIPTIONS, {"page": "0"}, **auth)

    assert resp.status_code == 400
    assert resp.json() == {"error": "Choose a page from 1 up."}


# --- POST /me/contact -> Flask POST /api/me/contact ------------------------------------


def test_contact_forwards_the_token_and_the_body_unchanged(client, auth, monkeypatch):
    """The "Talk to Minty" chat. Flask takes the sender from the token and validates the
    topic and message; nothing is checked or added here."""
    rec = _install(monkeypatch, body={"sent": True, "reply_to": "ada@example.com"})
    body = {"topic": "Technical issue", "message": "Help", "app": "minty-onboarding-web", "entity": "e-1"}

    resp = client.post(CONTACT, data=json.dumps(body), content_type="application/json", **auth)

    assert resp.status_code == 200
    assert resp.json() == {"sent": True, "reply_to": "ada@example.com"}
    [(method, url, kwargs)] = rec.calls
    assert method == "POST"
    assert url == f"{settings.PETTY_CASH_URL}/api/me/contact"
    assert kwargs["json"] == body
    assert kwargs["headers"]["Authorization"] == auth["HTTP_AUTHORIZATION"]


@pytest.mark.parametrize("status", [403, 422, 429, 502])
def test_contact_relays_flasks_refusal(client, auth, monkeypatch, status):
    refusal = {"error": "That's a lot of messages in one hour.", "fields": {}}
    _install(monkeypatch, status, refusal)

    resp = client.post(CONTACT, data="{}", content_type="application/json", **auth)

    assert resp.status_code == status
    assert resp.json() == refusal


# --- No token, no door --------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get(PROFILE),
        lambda c: c.patch(PROFILE, data="{}", content_type="application/json"),
        lambda c: c.get(SUBSCRIPTIONS),
        lambda c: c.post(CONTACT, data="{}", content_type="application/json"),
    ],
    ids=["GET profile", "PATCH profile", "GET subscriptions", "POST contact"],
)
def test_each_needs_a_token_and_calls_nobody_without_one(client, monkeypatch, call):
    """The proxies forward the caller's OWN token, so without one they must not reach
    upstream at all -- the API's default auth answers 401 first."""
    rec = _install(monkeypatch)

    assert call(client).status_code == 401
    assert rec.calls == []
