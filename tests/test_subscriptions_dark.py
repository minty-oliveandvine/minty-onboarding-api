"""The subscription switch off (``SUBSCRIPTION_ENABLED``, config.settings): the state the
production cutover runs in. /plans is empty and says why; /state says so; nothing else
changes. The proxied billing routes answer whatever Flask answers (404 while dark) and are
pinned on the Flask side (Minty/tests/test_char_subscription_dark.py)."""

import pytest
from django.test import override_settings

pytestmark = pytest.mark.django_db

PLANS = "/api/onboarding/plans"
STATE = "/api/onboarding/state"


@override_settings(SUBSCRIPTION_ENABLED=False)
def test_plans_is_empty_while_dark(client, auth, plans, modules, policy):
    resp = client.get(PLANS, **auth)
    assert resp.status_code == 200
    assert resp.json() == {"plans": [], "subscriptions_enabled": False}


def test_plans_says_it_is_live_when_on(client, auth, plans, modules, policy):
    body = client.get(PLANS, **auth).json()
    assert body["subscriptions_enabled"] is True and body["plans"]


@override_settings(SUBSCRIPTION_ENABLED=False)
def test_state_carries_the_switch(client, auth, user, countries, currencies, modules):
    resp = client.post("/api/onboarding/create", {"entity_name": "Dark Co", "country_id": "HK"},
                       content_type="application/json", **auth)
    assert resp.status_code == 201, resp.content
    body = client.get(STATE, {"entity_id": resp.json()["entity_id"]}, **auth).json()
    assert body["subscriptions_enabled"] is False
    assert body["current_step"] == 2  # the module step is still the next one; it just quotes nothing
