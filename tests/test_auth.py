"""Auth is the contract with Flask. These tests pin both halves of it.

Half one: a token Flask minted must be accepted. Half two: everything else must be
refused with the body the wizard can read.

The scope test is the one to read first -- it pins a deliberate DIVERGENCE from Flask
rather than a port of it.
"""

import uuid

import pytest

from tests.conftest import make_token

PLANS = "/api/onboarding/plans"
PUBLIC = "/api/onboarding/countries"


@pytest.mark.django_db
def test_a_valid_flask_token_is_accepted(client, user, plans, modules, policy):
    resp = client.get(PLANS, HTTP_AUTHORIZATION=f"Bearer {make_token(user.id)}")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_wrong_scope_is_refused(client, user, plans, modules, policy):
    """The deliberate tightening.

    Flask mints onboarding tokens with scope='onboarding' but never checks the claim, so
    today ANY token signed with the shared secret opens this surface -- including the
    longer-lived ones minted for the Module 2 frontend. This service checks it.

    If this test ever has to be relaxed, flip core.auth.ACCEPT_ANY_SCOPE rather than
    deleting the check, so the decision stays visible.
    """
    token = make_token(user.id, scope="billing")
    resp = client.get(PLANS, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_missing_scope_is_refused(client, user, plans, modules, policy):
    """A token with no scope claim at all. Older Flask tokens could look like this."""
    token = make_token(user.id, scope=None)
    resp = client.get(PLANS, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_a_token_signed_with_another_key_is_refused(client, user, plans, modules, policy):
    """The SECRET_KEY mismatch case -- the single most likely deployment fault.

    If this service and Flask are handed different secrets, every request 401s and the
    wizard reports it as an expired session, sending the user round a login loop that
    re-mints the same rejected token.
    """
    token = make_token(user.id, key="a-different-secret-entirely")
    resp = client.get(PLANS, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_expired_token_is_refused(client, user, plans, modules, policy):
    """These live 60 minutes and the wizard is a nine-step form. Expiry is routine."""
    token = make_token(user.id, minutes=-1)
    resp = client.get(PLANS, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_unknown_user_is_refused(client, plans, modules, policy):
    """A well-formed, correctly signed token naming a user who does not exist."""
    token = make_token(uuid.uuid4())
    resp = client.get(PLANS, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert resp.status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Bearer not.a.jwt",
        "Basic dXNlcjpwYXNz",
        "token abc123",
    ],
)
def test_malformed_authorization_headers_are_refused(
    client, header, plans, modules, policy
):
    resp = client.get(PLANS, HTTP_AUTHORIZATION=header)
    assert resp.status_code == 401


@pytest.mark.django_db
def test_every_401_uses_the_error_key_not_detail(client, user, plans, modules, policy):
    """THE SHAPE THE WIZARD READS.

    ninja's built-in auth failure renders ``{"detail": "Unauthorized"}``, and it happens
    before the endpoint is reached so none of the endpoint-level handlers see it. The
    wizard reads ``result.error`` at some twenty call sites and resolves copy from
    ``data.error ?? data.message`` -- a ``detail`` body is an undefined message and
    surfaces as a blank toast. Flask answers ``{"error": "Unauthorized"}`` here.

    This is regression cover for core.exceptions.on_unauthorized. Deleting that handler
    breaks nothing visible in a 401 status code, which is exactly why this asserts the
    body.
    """
    for header in ["", f"Bearer {make_token(user.id, scope='nope')}"]:
        resp = client.get(PLANS, HTTP_AUTHORIZATION=header)
        assert resp.status_code == 401
        body = resp.json()
        assert body == {"error": "Unauthorized"}, body
        assert "detail" not in body


@pytest.mark.django_db
def test_reference_endpoints_stay_public(client, countries):
    """Ported from Flask, and load-bearing for a cold resume.

    The registries render Step 1 before the wizard has necessarily settled a token.
    Gating them would break resuming in a fresh browser, so the split between public
    reference data and the token-gated catalog is kept exactly as Flask has it.
    """
    resp = client.get(PUBLIC)
    assert resp.status_code == 200
    assert resp.json()["countries"]


@pytest.mark.django_db
def test_public_endpoints_ignore_a_bad_token(client, countries):
    """A public endpoint must not start failing because a stale token came along.

    The wizard attaches its token to everything it sends. If an expired one turned the
    country list into a 401, Step 1 would go blank on resume rather than reloading.
    """
    resp = client.get(PUBLIC, HTTP_AUTHORIZATION="Bearer garbage.token.here")
    assert resp.status_code == 200
