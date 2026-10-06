"""The only module in this service that talks to minty-subscription-api.

minty-subscription-api is the subscription engine and the one writer of Stripe and the
subscription tables: cards, billing accounts, billing consent and trials. A second writer
there is a money bug, so this service never touches them -- it forwards.

WHAT GOES THROUGH HERE

  * Group G  GET  /api/onboarding/payment-method
             /api/onboarding/billing/*
             POST /api/onboarding/trials/start      (finalize's second half)

The transport is ``core.minty_client``'s -- the caller's own bearer forwarded verbatim,
the same timeout and the same 502/504 answers -- pointed at ``SUBSCRIPTION_API_URL``.
The subscription API applies its own membership check to the forwarded token, so this
service adds no privilege of its own.
"""

from django.conf import settings

from core import minty_client


def forward(request, path: str, *, method: str = "POST", json=None, params=None):
    """``(payload, status)`` from the subscription API, as the current caller."""
    return minty_client.forward(
        request, path, method=method, json=json, params=params,
        base=settings.SUBSCRIPTION_API_URL,
    )


def proxy(request, path: str, *, method: str = "POST", json=None, params=None):
    """:func:`forward`, as a ``JsonResponse`` a ninja view can return directly."""
    return minty_client.proxy(
        request, path, method=method, json=json, params=params,
        base=settings.SUBSCRIPTION_API_URL,
    )
