"""The one log line (minty_shared.logctx) as this service writes it: a trace per request, taken
from a well-formed ``X-Request-ID`` and echoed back; the person bound by the auth layer, the
company only once the entity gate has confirmed membership; and the trace sent on every call to
Flask, never to Xero."""

from __future__ import annotations

import logging
import re
import uuid
from types import SimpleNamespace

import pytest
from minty_shared import logctx

from core import minty_client, permissions, xero_tokens
from core.exceptions import AccessDeniedError

HEX8 = re.compile(r"[0-9a-f]{8}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.addFilter(logctx.ContextFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def lines():
    capture = _Capture()
    loggers = [logging.getLogger("minty-onboarding"), logging.getLogger("minty-onboarding.http")]
    for lg in loggers:
        lg.addHandler(capture)
    yield capture.records
    for lg in loggers:
        lg.removeHandler(capture)


@pytest.fixture(autouse=True)
def _clean():
    logctx.reset()
    yield
    logctx.reset()


def _access(records):
    (record,) = [r for r in records if r.name == "minty-onboarding.http"]
    return record


def test_well_formed_trace_is_kept_and_echoed(client, lines):
    res = client.get("/health", HTTP_X_REQUEST_ID="deadbeef")
    assert res["X-Request-ID"] == "deadbeef"
    record = _access(lines)
    assert (record.trace, record.step) == ("deadbeef", "health")
    assert re.fullmatch(r"GET /health 200 \d+ms", record.getMessage())


@pytest.mark.parametrize("incoming", [None, "short", "DEADBEEF", "deadbeef\nINFO forged"])
def test_a_missing_or_malformed_trace_is_replaced(client, lines, incoming):
    headers = {"HTTP_X_REQUEST_ID": incoming} if incoming is not None else {}
    res = client.get("/health", **headers)
    assert HEX8.fullmatch(res["X-Request-ID"])
    assert _access(lines).trace == res["X-Request-ID"]


@pytest.mark.django_db
def test_an_authenticated_request_names_the_person_and_its_route(client, auth, user, lines):
    res = client.get("/api/onboarding/state", **auth)  # no entity_id: 400 before any query
    assert res.status_code == 400
    record = _access(lines)
    assert (record.user, record.entity) == (str(user.id), "-")
    assert record.step == "get_state"
    assert logctx.current()["trace"] == "-"


@pytest.mark.django_db
def test_the_entity_gate_binds_a_member_only(user, other_user, entity):
    logctx.start_trace()
    with pytest.raises(AccessDeniedError):
        permissions.entity_for_member(other_user.id, entity.id)
    assert logctx.current()["entity"] == "-"
    permissions.entity_for_member(user.id, entity.id)
    assert logctx.current()["entity"] == str(entity.id)


def test_calls_to_flask_carry_the_trace(monkeypatch, settings):
    import requests

    sent = []

    def fake_request(method, url, headers=None, **kwargs):
        sent.append(dict(headers or {}))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    def fake_post(url, headers=None, **kwargs):
        sent.append(dict(headers or {}))
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "t"})

    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setattr(requests, "post", fake_post)
    settings.PETTY_CASH_URL = "http://flask.test"
    settings.XERO_TOKEN_SERVICE_URL = "http://flask.test/api/internal/xero/token"
    logctx.start_trace("a1b2c3d4")
    minty_client.forward(SimpleNamespace(headers={"Authorization": "Bearer t"}), "/api/x")
    xero_tokens.access_token_for(str(uuid.uuid4()))
    assert len(sent) == 2
    assert all(h.get("X-Request-ID") == "a1b2c3d4" for h in sent)


def test_the_call_to_xero_itself_sends_no_trace(monkeypatch):
    import requests

    sent = []
    monkeypatch.setattr(xero_tokens, "access_token_for", lambda _eid: "token")

    def fake_get(url, headers=None, **kwargs):
        sent.append(dict(headers or {}))
        return SimpleNamespace(status_code=200, json=lambda: [])

    monkeypatch.setattr(requests, "get", fake_get)
    logctx.start_trace("a1b2c3d4")
    xero_tokens.connected_tenant_ids(str(uuid.uuid4()))
    assert sent and all("X-Request-ID" not in h for h in sent)
