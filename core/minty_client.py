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

import logging

import requests
from django.conf import settings
from django.http import JsonResponse

from core.exceptions import HOUSE_FALLBACK, UpstreamError

logger = logging.getLogger("minty-onboarding")

#: What we say when Flask is unreachable or answers in a shape we cannot read. Cause-
#: neutral on purpose: the caller cannot act on "upstream 502" and the wizard renders
#: whatever is in ``error`` straight into a toast. Aliased rather than re-typed, so the
#: house sentence has exactly one definition.
UNREACHABLE = HOUSE_FALLBACK


def _url(path: str, base: str | None = None) -> str:
    return f"{base or settings.PETTY_CASH_URL}/{path.lstrip('/')}"


def bearer_from(request) -> str:
    """The caller's Authorization header, verbatim.

    Raises rather than returning empty: every path that proxies has already been
    through OnboardingBearerAuth, so a missing header here is a programming error in
    this service, not a client problem.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise UpstreamError(UNREACHABLE, status=500)
    return header


def forward(request, path: str, *, method: str = "POST", json=None, params=None, base=None):
    """Call Flask (or ``base``) as the current caller and return ``(payload, status)``.

    The status is Flask's own, not flattened -- a 402 from a declined card, a 403 on
    missing billing consent and a 409 on a Xero org mismatch all mean something specific
    to the wizard, and it has copy for each. Collapsing them into 502 would turn a
    "your card was declined" into "something went wrong".

    Returns the parsed body and the status. Callers hand both straight back to ninja.
    """
    url = _url(path, base)
    headers = {
        "Authorization": bearer_from(request),
        "Accept": "application/json",
    }
    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=json,
            params=params,
            timeout=settings.MINTY_PROXY_TIMEOUT,
        )
    except requests.Timeout:
        logger.warning("Proxy timeout: %s %s", method.upper(), url)
        raise UpstreamError(UNREACHABLE, status=504) from None
    except requests.RequestException as exc:
        logger.warning("Proxy failed: %s %s -- %s", method.upper(), url, exc)
        raise UpstreamError(UNREACHABLE, status=502) from None

    try:
        payload = resp.json()
    except ValueError:
        # Flask answered with something that is not JSON -- an HTML error page, or a
        # login redirect body if a route lost its csrf exemption. Do not pass the HTML
        # on: the wizard would render markup in a toast.
        logger.warning(
            "Proxy returned non-JSON: %s %s -> %s", method.upper(), url, resp.status_code
        )
        raise UpstreamError(UNREACHABLE, status=502) from None

    return payload, resp.status_code

def proxy(request, path: str, *, method: str = "POST", json=None, params=None, base=None):
    """:func:`forward`, wrapped as a ``JsonResponse`` a ninja view can return directly.

    WHY NOT JUST RETURN THE TUPLE: ninja refuses any status the decorator has no declared
    schema for, and turns the refusal into a 500. A proxy can legitimately relay 400, 402,
    403, 404, 409, 502, 503 -- enumerating all of those on every handler is noise, and
    missing one converts a meaningful answer into "something went wrong".

    An ``HttpResponse`` sidesteps the response-model machinery entirely: ninja passes it
    through untouched, so Flask's status and body reach the wizard exactly as sent. Which is
    the entire job of a proxy.
    """
    payload, status = forward(
        request, path, method=method, json=json, params=params, base=base
    )
    return JsonResponse(payload, status=status, safe=False)
