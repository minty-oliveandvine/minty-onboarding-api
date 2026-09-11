"""Obtain a Xero access token from Flask. Never refresh one.

THE DISTINCTION THAT MATTERS: USING vs REFRESHING

The write rule says Flask owns the Xero rail, and it is easy to read that as "this
service must never speak to Xero at all". That is stricter than the actual constraint,
and stricter than what billing-backend does.

The constraint is that only ONE service may call Xero's ``/connect/token``. Xero rotates
the refresh token on every use and invalidates the previous one, so a second refresher
POSTs a spent token, gets ``400 invalid_grant``, and the customer's connection stays
broken until they reconnect by hand. Flask is that one service, and it serialises
refreshes behind an advisory lock.

Using an access token Flask already minted is different: it is a read against Xero's API
with a credential somebody else is responsible for keeping alive. billing-backend does
exactly this to publish bills. So does this module.

Concretely: ASK for a token here, then call Xero. Never put XERO_CLIENT_ID or
XERO_CLIENT_SECRET in this service's settings -- that is the line, and it is the one
billing-backend's settings.py draws too.

THE ASSERTION

``entity_id`` travels inside the SIGNED claims, not the request body, so a leaked
assertion cannot be replayed against a different entity. Flask reads it from the claims
and ignores the body entirely. Sixty-second expiry, HS256, shared SECRET_KEY, scope
``xero-access-token`` -- the shape billing-backend already uses, so Flask's endpoint needs
no change to serve this service.
"""

import logging
from datetime import datetime, timedelta, timezone

import jwt
import requests
from django.conf import settings

logger = logging.getLogger("minty-onboarding")

#: The scope Flask's /api/internal/xero/token requires. A different value is refused.
TOKEN_SERVICE_SCOPE = "xero-access-token"


def _token_service_url() -> str:
    """Flask's internal token endpoint. Defined in settings; see the note there."""
    return settings.XERO_TOKEN_SERVICE_URL


def access_token_for(entity_id: str) -> str | None:
    """A currently-valid Xero access token for ``entity_id``, or None.

    None means "cannot talk to Xero right now", and every caller must treat it as
    inconclusive rather than as "not connected". The difference is important: this
    returns None both when the connection genuinely needs a human to reconnect (Flask
    answers 409) and when the token service is simply unreachable, and clearing local
    connection state on the second case would drop a live Xero connection over a network
    blip.

    Never raises. A token-service outage must not turn a wizard resume into a 500.
    """
    url = _token_service_url()
    secret = settings.SECRET_KEY
    if not url or not secret:
        logger.error("xero: token service URL or SECRET_KEY unset; cannot get a token")
        return None

    now = datetime.now(tz=timezone.utc)
    assertion = jwt.encode(
        {
            "scope": TOKEN_SERVICE_SCOPE,
            # In the claims, deliberately -- see the module header.
            "entity_id": str(entity_id),
            "iat": now,
            "exp": now + timedelta(seconds=60),
        },
        secret,
        algorithm="HS256",
    )

    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {assertion}"},
            timeout=settings.XERO_TOKEN_SERVICE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("xero: token service unreachable for entity %s: %s", entity_id, exc)
        return None

    if resp.status_code == 409:
        # Flask's considered answer: no usable token, or a concurrent refresh is in
        # flight. Not an error, and not proof the org was disconnected.
        logger.info("xero: reconnect required for entity %s", entity_id)
        return None

    if resp.status_code != 200:
        logger.warning(
            "xero: token service error for entity %s: %s %s",
            entity_id,
            resp.status_code,
            (resp.text or "")[:200],
        )
        return None

    try:
        token = (resp.json() or {}).get("access_token")
    except ValueError:
        logger.warning("xero: token service returned malformed JSON for %s", entity_id)
        return None

    if not token:
        logger.warning("xero: token service returned no access_token for %s", entity_id)
        return None
    return token


#: Xero's tenant list. The only Xero endpoint this service calls.
CONNECTIONS_URL = "https://api.xero.com/connections"


def connected_tenant_ids(entity_id: str) -> set[str] | None:
    """Tenant ids Xero currently reports for this entity's connection, or None.

    None means "could not verify" and is NOT an empty set. Callers must not treat the two
    the same: an empty set is Xero saying the org is gone, while None is this service
    saying it does not know.
    """
    token = access_token_for(entity_id)
    if token is None:
        return None

    try:
        resp = requests.get(
            CONNECTIONS_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("xero: /connections failed for entity %s: %s", entity_id, exc)
        return None

    if resp.status_code != 200:
        logger.warning(
            "xero: /connections returned %s for entity %s; preserving existing state",
            resp.status_code,
            entity_id,
        )
        return None

    try:
        payload = resp.json() or []
    except ValueError:
        logger.warning("xero: /connections returned malformed JSON for %s", entity_id)
        return None

    return {
        str(conn.get("tenantId"))
        for conn in payload
        if isinstance(conn, dict) and conn.get("tenantId")
    }
