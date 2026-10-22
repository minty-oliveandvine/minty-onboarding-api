"""The one error envelope every Minty API answers with (minty_shared.django.errors, user
2026-10-22): ``{"error": <sentence>, "fields"?: {...}}``; input the schema refuses is 422 (it was
400 here) naming each field; the wizard's own refusals keep their statuses; 401 ``Unauthorized``;
500 the house sentence."""

from __future__ import annotations

import json

import pytest
from django.test import RequestFactory

HOUSE = "Something went wrong on my end. Mind trying again?"


def _handled(exc):
    from config.urls import api

    request = RequestFactory().get("/api/onboarding/state")
    response = api._lookup_exception_handler(exc)(request, exc)
    return response.status_code, json.loads(response.content)


@pytest.mark.django_db
def test_no_token_is_401_unauthorized(client):
    resp = client.get("/api/onboarding/state")
    assert (resp.status_code, resp.json()) == (401, {"error": "Unauthorized"})


def test_an_unhandled_failure_is_the_house_sentence():
    assert _handled(RuntimeError("database fell over: secret-detail")) == (500, {"error": HOUSE})


def test_the_wizards_refusals_keep_their_statuses():
    from core.exceptions import (
        AccessDeniedError,
        ConflictError,
        NotFoundError,
        OnboardingValidationError,
        UpstreamError,
    )

    assert _handled(OnboardingValidationError("Entity name is required.")) == (
        400,
        {"error": "Entity name is required."},
    )
    assert _handled(AccessDeniedError("You don't have access to this entity")) == (
        403,
        {"error": "You don't have access to this entity"},
    )
    assert _handled(NotFoundError("Entity not found")) == (404, {"error": "Entity not found"})
    assert _handled(ConflictError("That name is taken.")) == (409, {"error": "That name is taken."})
    assert _handled(UpstreamError("Your card was declined.", 402)) == (
        402,
        {"error": "Your card was declined."},
    )


def test_schema_validation_is_422_naming_each_field():
    from ninja.errors import ValidationError

    exc = ValidationError([{"type": "missing", "loc": ("body", "payload", "entity_name"), "msg": "x"}])
    assert _handled(exc) == (
        422,
        {
            "error": "I still need entity name to continue.",
            "fields": {"entity_name": "The entity name is still empty. Add the entity name to continue."},
        },
    )
