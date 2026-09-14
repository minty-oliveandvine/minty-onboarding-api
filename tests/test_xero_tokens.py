"""core/xero_tokens -- obtain a Xero token from Flask, then read Xero's tenant list.

Until now every caller stubbed ``connected_tenant_ids`` whole, so none of this had run
under test: not the assertion Flask receives, not the 409 "reconnect required" branch,
not the difference between None and an empty set that ``services/state.py`` depends on.
These stub ``requests.post`` (the token service) and ``requests.get`` (Xero) and let the
module itself run.

THE CONTRACT UNDER TEST is the asymmetry in the return value:

    None        could not verify -- token service down, 409, non-200, bad JSON
    set()       Xero answered, and the org is gone
    {ids}       Xero answered, and these are the tenants

Callers must never treat None like an empty set: that would clear a live Xero
connection over a network blip.
"""

import json

import jwt
import pytest
import requests
from django.conf import settings

from core import xero_tokens


class FakeResponse:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text if body is None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class Recorder:
    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raises:
            raise self.raises
        return self.response


@pytest.fixture
def token_service(monkeypatch):
    """Install a stub for Flask's token endpoint (``requests.post``)."""

    def _install(response=None, raises=None):
        r = Recorder(response=response, raises=raises)
        monkeypatch.setattr(requests, "post", r)
        return r

    return _install


@pytest.fixture
def xero(monkeypatch):
    """Install a stub for Xero's /connections (``requests.get``)."""

    def _install(response=None, raises=None):
        r = Recorder(response=response, raises=raises)
        monkeypatch.setattr(requests, "get", r)
        return r

    return _install


ENTITY = "2749a5a2-5a9f-482a-97df-af2b6a5ac0e6"


# --- access_token_for: the assertion --------------------------------------------------


def test_the_assertion_carries_entity_id_in_the_signed_claims(token_service):
    """entity_id travels in the CLAIMS, not the body, so a leaked assertion cannot be
    replayed against a different entity. Flask reads it from there and ignores the body."""
    r = token_service(FakeResponse(200, {"access_token": "xat"}))
    assert xero_tokens.access_token_for(ENTITY) == "xat"

    url, kwargs = r.calls[0]
    assert url == settings.XERO_TOKEN_SERVICE_URL
    assert "json" not in kwargs and "data" not in kwargs  # nothing in the body

    assertion = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["entity_id"] == ENTITY
    assert claims["scope"] == xero_tokens.TOKEN_SERVICE_SCOPE == "xero-access-token"


def test_the_assertion_is_short_lived(token_service):
    token_service(FakeResponse(200, {"access_token": "xat"}))
    r = token_service(FakeResponse(200, {"access_token": "xat"}))
    xero_tokens.access_token_for(ENTITY)
    assertion = r.calls[0][1]["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(assertion, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 60


def test_the_assertion_uses_the_token_service_timeout(token_service):
    r = token_service(FakeResponse(200, {"access_token": "xat"}))
    xero_tokens.access_token_for(ENTITY)
    assert r.calls[0][1]["timeout"] == settings.XERO_TOKEN_SERVICE_TIMEOUT


def test_the_assertion_scope_is_not_the_onboarding_scope():
    # A different scope from the launch token on purpose: OnboardingBearerAuth verifies
    # scope, so a leaked token-service assertion cannot open the wizard API, and vice
    # versa.
    assert xero_tokens.TOKEN_SERVICE_SCOPE != "onboarding"


# --- access_token_for: every failure is None, never an exception ----------------------


def test_token_service_unreachable_is_none(token_service):
    token_service(raises=requests.ConnectionError("refused"))
    assert xero_tokens.access_token_for(ENTITY) is None


def test_token_service_timeout_is_none(token_service):
    token_service(raises=requests.Timeout())
    assert xero_tokens.access_token_for(ENTITY) is None


def test_409_reconnect_required_is_none(token_service):
    """Flask's considered answer: no usable token, or a concurrent refresh in flight.
    Not an error -- and NOT proof the org was disconnected."""
    token_service(FakeResponse(409, {"error": "reconnect required"}))
    assert xero_tokens.access_token_for(ENTITY) is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
def test_any_other_non_200_is_none(token_service, status):
    token_service(FakeResponse(status, {"error": "x"}))
    assert xero_tokens.access_token_for(ENTITY) is None


def test_malformed_json_from_the_token_service_is_none(token_service):
    token_service(FakeResponse(200, None, text="<html>"))
    assert xero_tokens.access_token_for(ENTITY) is None


@pytest.mark.parametrize("body", [{}, {"access_token": ""}, {"access_token": None}, {"token": "wrong key"}])
def test_a_200_without_an_access_token_is_none(token_service, body):
    token_service(FakeResponse(200, body))
    assert xero_tokens.access_token_for(ENTITY) is None


def test_missing_configuration_is_none_without_calling_out(token_service, settings):
    r = token_service(FakeResponse(200, {"access_token": "xat"}))
    settings.XERO_TOKEN_SERVICE_URL = ""
    assert xero_tokens.access_token_for(ENTITY) is None
    assert r.calls == []


# --- connected_tenant_ids: None vs empty set ------------------------------------------


def test_no_token_means_none_and_xero_is_never_called(token_service, xero):
    token_service(FakeResponse(409, {}))
    x = xero()
    assert xero_tokens.connected_tenant_ids(ENTITY) is None
    assert x.calls == []


def test_xero_answers_with_tenants(token_service, xero):
    token_service(FakeResponse(200, {"access_token": "xat"}))
    x = xero(FakeResponse(200, [{"tenantId": "t-1"}, {"tenantId": "t-2"}]))
    assert xero_tokens.connected_tenant_ids(ENTITY) == {"t-1", "t-2"}
    url, kwargs = x.calls[0]
    assert url == xero_tokens.CONNECTIONS_URL
    assert kwargs["headers"]["Authorization"] == "Bearer xat"


def test_xero_answers_with_no_tenants_is_an_EMPTY_SET_not_none(token_service, xero):
    """The one case that must NOT be None: Xero answered, and the org is gone. This is
    the evidence services/state.py needs before it clears local connection state."""
    token_service(FakeResponse(200, {"access_token": "xat"}))
    xero(FakeResponse(200, []))
    result = xero_tokens.connected_tenant_ids(ENTITY)
    assert result == set()
    assert result is not None


def test_xero_unreachable_is_none(token_service, xero):
    token_service(FakeResponse(200, {"access_token": "xat"}))
    xero(raises=requests.ConnectionError("dns"))
    assert xero_tokens.connected_tenant_ids(ENTITY) is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_xero_non_200_is_none_and_preserves_state(token_service, xero, status):
    # A 401 here means the token Flask handed us was bad, not that the org is gone.
    token_service(FakeResponse(200, {"access_token": "xat"}))
    xero(FakeResponse(status, {"Title": "x"}))
    assert xero_tokens.connected_tenant_ids(ENTITY) is None


def test_xero_malformed_json_is_none(token_service, xero):
    token_service(FakeResponse(200, {"access_token": "xat"}))
    xero(FakeResponse(200, None, text="not json"))
    assert xero_tokens.connected_tenant_ids(ENTITY) is None


def test_tenant_rows_without_an_id_are_skipped(token_service, xero):
    token_service(FakeResponse(200, {"access_token": "xat"}))
    xero(FakeResponse(200, [{"tenantId": "t-1"}, {"tenantId": ""}, {"other": 1}, "junk", None]))
    assert xero_tokens.connected_tenant_ids(ENTITY) == {"t-1"}


def test_tenant_ids_are_strings(token_service, xero):
    # entities.xero_org_id is compared as str(); a uuid object here would never match.
    token_service(FakeResponse(200, {"access_token": "xat"}))
    xero(FakeResponse(200, [{"tenantId": 12345}]))
    assert xero_tokens.connected_tenant_ids(ENTITY) == {"12345"}


def test_nothing_in_this_module_refreshes_a_token():
    """The line the module header draws: ask for a token, never mint one. A second
    refresher would spend Flask's rotating refresh token and break the customer's
    connection until they reconnect by hand."""
    import inspect

    # The module docstring names both things it must not do; look past it at the code.
    code = inspect.getsource(xero_tokens).split('"""', 2)[2]
    assert "/connect/token" not in code
    assert "refresh_token" not in code
    assert "XERO_CLIENT_ID" not in code and "XERO_CLIENT_SECRET" not in code
