"""``/api/internal/chart/*``: a company's accounts and contacts for the hub's Settings (owner,
2026-11-12), reached only from another Minty backend - accounts-api, which checks the person first.

The caller proves itself with a short-lived service assertion (``minty_shared.tokens``, signed
with the shared ``SECRET_KEY``) for scope ``company-chart``: the company and the person are its
claims, never the body's. The person's rights are checked again here (``chart``)."""

from __future__ import annotations

import json
import logging

import jwt
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from minty_shared import logctx, tokens

from onboarding.services import chart

logger = logging.getLogger("minty-onboarding")

SCOPE = "company-chart"
UNAUTHORIZED = {"error": "Unauthorized"}
BAD_BODY = {"error": "Send the details as JSON."}


def _claims(request: HttpRequest) -> dict | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        claims = tokens.decode_bearer(header[7:].strip(), settings.SECRET_KEY,
                                      options={"require": ["exp", "scope", "entity_id", "user_id"]})
    except jwt.InvalidTokenError as exc:
        logger.warning("internal chart: rejected caller (%s)", exc)
        return None
    if claims.get("scope") != SCOPE:
        logger.warning("internal chart: rejected caller (wrong scope)")
        return None
    logctx.bind(user=claims["user_id"], entity=claims["entity_id"])
    return claims


def _body(request: HttpRequest) -> dict | None:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _door(handlers: dict):
    """A view answering the methods in ``handlers`` (method -> fn(user_id, entity_id, body, **kw))."""

    @csrf_exempt
    def view(request: HttpRequest, **kwargs):
        handler = handlers.get(request.method)
        if handler is None:
            return JsonResponse({"error": "That isn't something this address does."}, status=405)
        claims = _claims(request)
        if claims is None:
            return JsonResponse(UNAUTHORIZED, status=401)
        body = _body(request) if request.method in ("POST", "PATCH") else {}
        if body is None:
            return JsonResponse(BAD_BODY, status=422)
        answer, status = handler(claims["user_id"], claims["entity_id"], body, **kwargs)
        return JsonResponse(answer, status=status)

    return view


accounts = _door({
    "GET": lambda user, entity, _body: chart.accounts(user, entity),
    "POST": lambda user, entity, body: chart.add_account(user, entity, body),
})
account = _door({
    "PATCH": lambda user, entity, body, account_id: chart.edit_account(user, entity, account_id, body),
    "DELETE": lambda user, entity, _body, account_id: chart.delete_account(user, entity, account_id),
})
contacts = _door({
    "GET": lambda user, entity, _body: chart.contacts(user, entity),
    "POST": lambda user, entity, body: chart.add_contact(user, entity, body),
})
contact = _door({
    "PATCH": lambda user, entity, body, contact_id: chart.edit_contact(user, entity, contact_id, body),
    "DELETE": lambda user, entity, _body, contact_id: chart.delete_contact(user, entity, contact_id),
})
