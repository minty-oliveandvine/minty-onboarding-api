"""The only module in this service that talks to the Flask app -- and the transport
``core/subscription_client.py`` reuses for the subscription API.

WHY IT IS THE ONLY ONE

The boundary this service is built on is "whoever owns the external rail owns the
write". Xero has exactly one legitimate writer, and it is Flask: Xero rotates its
refresh token on every use and invalidates the previous one. Two services refreshing
means one of them POSTs a spent token, gets 400 invalid_grant, and the customer's Xero
connection stays broken until they manually reconnect. minty-payment-request-api/config/
settings.py makes the same call for the same reason and leaves XERO_CLIENT_ID empty on
purpose. (Stripe's one writer is minty-subscription-api; see subscription_client.py.)

A rule spread across a dozen service modules is a rule nobody can check. Concentrating
every outbound call here means the rule is verified by reading one file, and a new
Xero call cannot appear anywhere else without looking obviously wrong.

WHAT GOES THROUGH HERE (to Flask)

  * Group F  POST /api/onboarding/modules            (module grant)
  * Group G  POST /api/onboarding/xero/disconnect    (needs a Xero token)
  * Group G  POST /api/onboarding/xero/release       (frees another company's org)
  * Group D  the Xero chart of accounts and contacts
  * Group E  POST /api/onboarding/invite             (the email links into Flask)

HOW IT AUTHENTICATES

It forwards the caller's own bearer token unchanged. That is the important choice: the
proxy carries no privilege of its own, so Flask applies exactly the membership and
consent checks it would have applied to a direct call, and a caller cannot reach through
this service to do something Flask would have refused. There is no service-to-service
credential here to leak or to scope wrongly.
"""

from django.conf import settings
from django.http import JsonResponse
from minty_shared.django import client as shared_client

from core.exceptions import HOUSE_FALLBACK, UpstreamError

#: What we say when Flask is unreachable or answers in a shape we cannot read. Cause-
#: neutral on purpose: the caller cannot act on "upstream 502" and the wizard renders
#: whatever is in ``error`` straight into a toast. Aliased rather than re-typed, so the
#: house sentence has exactly one definition.
UNREACHABLE = HOUSE_FALLBACK


def bearer_from(request) -> str:
    """The caller's Authorization header, verbatim (``minty_shared.django.client``)."""
    try:
        return shared_client.bearer_from(request)
    except shared_client.UpstreamUnavailable as exc:
        raise UpstreamError(exc.message, status=exc.status) from None


def forward(request, path: str, *, method: str = "POST", json=None, params=None, base=None):
    """Call Flask (or ``base``) as the current caller and return ``(payload, status)``.

    The status is Flask's own, not flattened -- a 402 from a declined card, a 403 on
    missing billing consent and a 409 on a Xero org mismatch all mean something specific
    to the wizard, and it has copy for each. The transport, the trace header and the
    502/504 for no usable answer are ``minty_shared.django.client``'s.
    """
    try:
        return shared_client.forward(
            request,
            path,
            base=base or settings.PETTY_CASH_URL,
            timeout=settings.MINTY_PROXY_TIMEOUT,
            method=method,
            json=json,
            params=params,
        )
    except shared_client.UpstreamUnavailable as exc:
        raise UpstreamError(exc.message, status=exc.status) from None


def proxy(request, path: str, *, method: str = "POST", json=None, params=None, base=None):
    """:func:`forward`, wrapped as a ``JsonResponse`` a ninja view can return directly.

    WHY NOT JUST RETURN THE TUPLE: ninja refuses any status the decorator has no declared
    schema for, and turns the refusal into a 500. A proxy can legitimately relay 400, 402,
    403, 404, 409, 502, 503 -- an ``HttpResponse`` sidesteps the response-model machinery,
    so Flask's status and body reach the wizard exactly as sent.
    """
    payload, status = forward(request, path, method=method, json=json, params=params, base=base)
    return JsonResponse(payload, status=status, safe=False)
