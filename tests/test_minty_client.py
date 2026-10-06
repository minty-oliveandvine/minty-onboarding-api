"""core/minty_client -- the only outbound path to Flask.

Every other test that touches a proxied endpoint stubs ``minty_client.forward`` itself,
which is why this module's error branches had never run: a stub at our own function is a
stub of the thing under test. These stub at the ``requests`` boundary instead, so the
timeout, the connection failure, the non-JSON body and the status pass-through are all
exercised for real.

The property that matters most is the LAST one: Flask's status reaches the wizard as
itself. A 402 from a declined card and a 409 from a Xero org mismatch each have their own
copy in the wizard, and a proxy that flattened them to 502 would turn "your card was
declined" into "something went wrong".
"""

import json

import pytest
import requests
from django.conf import settings

from core import minty_client
from core.exceptions import HOUSE_FALLBACK, UpstreamError


class FakeResponse:
    """Just enough of ``requests.Response`` for ``forward`` to read."""

    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text if body is None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class Recorder:
    """Stands in for ``requests.request`` and remembers the call it received."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.raises:
            raise self.raises
        return self.response


class Req:
    """A request carrying only what ``bearer_from`` reads."""

    def __init__(self, authorization="Bearer abc.def.ghi"):
        self.headers = {"Authorization": authorization} if authorization else {}


@pytest.fixture
def rec(monkeypatch):
    def _install(response=None, raises=None):
        r = Recorder(response=response, raises=raises)
        monkeypatch.setattr(requests, "request", r)
        return r

    return _install


# --- bearer_from -----------------------------------------------------------------------


def test_bearer_is_forwarded_verbatim():
    assert minty_client.bearer_from(Req("Bearer tok.en.x")) == "Bearer tok.en.x"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "bearer lowercase", "Token x"])
def test_a_missing_or_non_bearer_header_is_a_programming_error(header):
    # Every proxied path has already been through auth, so this cannot be a client
    # problem -- it is a 500, not a 401.
    with pytest.raises(UpstreamError) as e:
        minty_client.bearer_from(Req(header))
    assert e.value.status == 500


# --- forward: the happy path -----------------------------------------------------------


def test_forward_returns_flask_body_and_status(rec):
    rec(FakeResponse(200, {"ok": True}))
    payload, status = minty_client.forward(Req(), "/api/onboarding/finalize", json={"a": 1})
    assert payload == {"ok": True}
    assert status == 200


def test_forward_builds_the_flask_url_from_settings(rec):
    r = rec(FakeResponse(200, {}))
    minty_client.forward(Req(), "api/onboarding/modules")
    minty_client.forward(Req(), "/api/onboarding/modules")
    # With or without a leading slash, the same URL -- one slash between base and path.
    urls = {url for _, url, _ in r.calls}
    assert urls == {f"{settings.PETTY_CASH_URL}/api/onboarding/modules"}


def test_forward_sends_the_callers_own_token_and_asks_for_json(rec):
    r = rec(FakeResponse(200, {}))
    minty_client.forward(Req("Bearer caller.token.here"), "/x")
    _, _, kwargs = r.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer caller.token.here"
    assert kwargs["headers"]["Accept"] == "application/json"
    # No service credential of its own. That is the design: the proxy carries no
    # privilege, so Flask applies exactly the checks it would on a direct call.
    assert set(kwargs["headers"]) == {"Authorization", "Accept"}


def test_forward_passes_method_json_params_and_timeout(rec):
    r = rec(FakeResponse(200, {}))
    minty_client.forward(Req(), "/x", method="get", json={"k": "v"}, params={"entity_id": "e"})
    method, _, kwargs = r.calls[0]
    assert method == "GET"  # upper-cased
    assert kwargs["json"] == {"k": "v"}
    assert kwargs["params"] == {"entity_id": "e"}
    assert kwargs["timeout"] == settings.MINTY_PROXY_TIMEOUT


# --- forward: statuses pass through unflattened ----------------------------------------


@pytest.mark.parametrize("status", [400, 402, 403, 404, 409, 422, 500, 503])
def test_flasks_status_reaches_the_caller_as_itself(rec, status):
    rec(FakeResponse(status, {"error": "as Flask said it"}))
    payload, got = minty_client.forward(Req(), "/x")
    assert got == status
    assert payload == {"error": "as Flask said it"}


# --- forward: failure branches ---------------------------------------------------------


def test_timeout_is_504_with_the_house_sentence(rec):
    rec(raises=requests.Timeout())
    with pytest.raises(UpstreamError) as e:
        minty_client.forward(Req(), "/x")
    assert e.value.status == 504
    assert str(e.value) == HOUSE_FALLBACK


def test_connection_failure_is_502_with_the_house_sentence(rec):
    rec(raises=requests.ConnectionError("refused"))
    with pytest.raises(UpstreamError) as e:
        minty_client.forward(Req(), "/x")
    assert e.value.status == 502
    assert str(e.value) == HOUSE_FALLBACK


def test_non_json_body_is_502_and_the_html_is_not_passed_on(rec):
    # An HTML error page, or a login redirect body if a route lost its csrf exemption.
    rec(FakeResponse(200, None, text="<html><body>Sign in</body></html>"))
    with pytest.raises(UpstreamError) as e:
        minty_client.forward(Req(), "/x")
    assert e.value.status == 502
    assert "<html" not in str(e.value)
    assert str(e.value) == HOUSE_FALLBACK


def test_the_house_sentence_is_cause_neutral():
    # The wizard renders `error` straight into a toast; "upstream 502" is not something
    # a user can act on, and the message is one definition shared with core.exceptions.
    assert minty_client.UNREACHABLE is HOUSE_FALLBACK
    assert "502" not in HOUSE_FALLBACK and "upstream" not in HOUSE_FALLBACK.lower()


# --- proxy: the JsonResponse wrapper ---------------------------------------------------


def test_proxy_returns_a_json_response_with_flasks_status(rec):
    rec(FakeResponse(409, {"error": "That Xero org is connected to another company."}))
    resp = minty_client.proxy(Req(), "/x")
    assert resp.status_code == 409
    assert json.loads(resp.content) == {"error": "That Xero org is connected to another company."}


def test_proxy_relays_a_list_body(rec):
    # `safe=False`: some Flask endpoints answer with a bare list, and JsonResponse
    # refuses those by default.
    rec(FakeResponse(200, [1, 2, 3]))
    resp = minty_client.proxy(Req(), "/x")
    assert json.loads(resp.content) == [1, 2, 3]


# --- through a real endpoint -----------------------------------------------------------


@pytest.mark.django_db
def test_a_proxied_endpoint_relays_a_timeout_as_504(client, auth, entity, monkeypatch):
    """End to end through ninja: the exception handler turns UpstreamError into the body
    shape the wizard expects, with `error` (never ninja's `detail`) and Flask's status."""
    monkeypatch.setattr(requests, "request", Recorder(raises=requests.Timeout()))
    resp = client.post(
        "/api/onboarding/modules",
        data=json.dumps({"entity_id": entity.id}),
        content_type="application/json",
        **auth,
    )
    assert resp.status_code == 504
    assert resp.json() == {"error": HOUSE_FALLBACK}


@pytest.mark.django_db
def test_a_proxied_endpoint_relays_flasks_own_answer(client, auth, entity, monkeypatch):
    monkeypatch.setattr(
        requests, "request", Recorder(FakeResponse(402, {"error": "Your card was declined."}))
    )
    resp = client.post(
        "/api/onboarding/billing/authorize",
        data=json.dumps({"entity_id": entity.id}),
        content_type="application/json",
        **auth,
    )
    assert resp.status_code == 402
    assert resp.json() == {"error": "Your card was declined."}
