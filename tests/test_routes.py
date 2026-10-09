"""The route table itself, and which side of the write rule each endpoint falls on.

WHY THIS EXISTS: a router file can be written, imported, and never registered. That failure
is silent -- the service boots, ``manage.py check`` is clean, and every endpoint in the group
answers 404 as though it had never been ported. It happened once, during Group D, because a
scripted edit added the import and not the ``add_router`` line.

It has a second job now. Every ``/api/onboarding/*`` path Flask serves is answered here, but
only some are actually IMPLEMENTED here -- the rest are proxies. From outside, the two look
identical. PORTED and PROXIED below are the machine-readable record of which is which, so
"what does this service actually own" has one answer that cannot drift from the code.

Adding an endpoint means adding it to one of the two sets. Deciding which is the point.

ENTRIES ARE PER METHOD, not per path. That is not pedantry: ``/invite`` is split down the
middle -- GET lists invitations locally, POST sends one and is proxied, because the email it
sends links into a Flask route. A set keyed by path could not say that, and the first version
of this file quietly mis-stated it.
"""

import importlib

import pytest

#: Implemented in this service. Reads and writes go straight to Postgres.
PORTED = {
    # Group A -- reference data
    "GET /server-time",
    "GET /currencies",
    "GET /countries",
    "GET /plans",
    # Group B -- wizard state
    "GET /state",
    "POST /saved-step",
    # Group C -- the company
    "POST /create",
    "PUT /entity/{entity_id}",
    # Group D1 -- petty-cash config with no Xero in it
    "GET /sales-methods",
    "POST /sales-methods",
    "POST /opening-balance",
    # Group E -- listing is local; POST /invite is NOT (see PROXIED).
    "GET /invite",
    "POST /invite/cancel",
    # Group G -- finalize flips the company here; its trial start is the subscription API's.
    "POST /finalize",
}

#: Answered here, executed elsewhere. Each touches a rail another service owns -- Stripe and
#: subscription state (minty-subscription-api), a Xero token or a Flask-owned URL (Flask) --
#: so duplicating it would mean two writers against something that permits only one.
PROXIED = {
    # Group D2 -- Xero chart of accounts, Xero contacts, minty-payment-request-api's bill codes
    "GET /account-codes",
    "POST /account-codes",
    "POST /contacts",
    "POST /contacts/create",
    "GET /bill-codes",
    "POST /bill-codes",
    # Group E -- sending. The email links into a Flask route and uses a Flask template, so
    # a URL built here would be a guess at someone else's routing that fails silently.
    "POST /invite",
    # Group F -- enabling a module is a subscription write, not an entity write
    "POST /modules",
    # Group G -- Stripe and billing consent (subscription API), the Xero disconnect (Flask)
    "GET /payment-method",
    "GET /billing/payment-methods",
    "POST /billing/payment-methods/setup-intent",
    "POST /billing/payment-methods/confirm",
    "POST /billing/payment-methods/default",
    "GET /billing/accounts",
    "POST /billing/accounts",
    "POST /billing/authorize",
    "POST /xero/disconnect",
    "POST /xero/release",
}

EXPECTED = PORTED | PROXIED


def _operations():
    """{"METHOD path": handler module} for every registered operation, one per method."""
    from config.urls import api

    found: dict[str, str] = {}
    for prefix, router in api._routers:
        for path, pv in router.path_operations.items():
            full = f"{prefix.rstrip('/')}{path}"
            for op in pv.operations:
                for method in op.methods:
                    found[f"{method} {full}"] = op.view_func.__module__
    return found


def test_every_expected_route_is_registered():
    missing = EXPECTED - set(_operations())
    assert not missing, (
        f"routes missing from the API: {sorted(missing)}. "
        "A router file can exist and be imported without being registered -- check that "
        "config/urls.py calls api.add_router for it."
    )


def test_no_unexpected_routes_are_exposed():
    """A new endpoint must be a deliberate addition, not a surprise.

    This surface is reached cross-origin with a bearer token, so an accidentally exposed
    handler is a security question and not merely an untidiness.
    """
    extra = set(_operations()) - EXPECTED
    assert not extra, (
        f"unexpected routes exposed: {sorted(extra)}. Add each to PORTED or PROXIED."
    )


def test_ported_and_proxied_do_not_overlap():
    """An endpoint is one or the other. Both would mean nobody knows which runs."""
    assert not (PORTED & PROXIED)


@pytest.mark.parametrize("route", sorted(PROXIED))
def test_every_proxied_route_goes_through_an_outbound_module(route):
    """``core/minty_client.py`` (Flask) and ``core/subscription_client.py`` (the subscription
    API) are the ONLY modules allowed to call another service.

    That is what makes the write rule checkable by reading two files. A handler that called
    ``requests`` directly would still work, and would quietly reopen the boundary -- so the
    check is on the import, not on the behaviour.
    """
    name = _operations().get(route)
    assert name, f"no handler found for {route}"
    source = importlib.import_module(name)
    assert hasattr(source, "minty_client") or hasattr(source, "subscription_client"), (
        f"{name} serves a proxied route but imports neither core.minty_client nor "
        "core.subscription_client. Every outbound call must go through one of them."
    )


@pytest.mark.parametrize("route", sorted(EXPECTED))
def test_paths_carry_no_version_or_extra_prefix(route):
    """Paths match Flask byte for byte -- that is what makes the cutover a base-URL swap.

    A path like ``/api/v1/onboarding/...`` would force the frontend to rewrite paths per
    group and undo the whole cutover mechanism.
    """
    path = route.split(" ", 1)[1]
    assert path.startswith("/")
    assert "/v1/" not in path
    assert not path.startswith("/onboarding/")


def test_the_whole_flask_surface_is_answered():
    """Every Flask onboarding endpoint is reachable here, ported or proxied.

    This is what lets the wizard point at a single base URL. If the total drops, the frontend
    needs a per-path exception again -- the thing ``lib/apiRoutes.js`` exists to avoid.

    The split is the honest headline: well under half of the surface is actually implemented
    here, and that is by design, not by how far the work got. The remainder unblocks when
    ``subscription-service`` and ``xero-service`` exist.

    20 -> 18 on 2026-10-01, deliberately: ``POST /payment-method/setup`` and ``/complete``
    opened Stripe's HOSTED Checkout, and a card is now only ever captured in-app onto a
    billing account. The wizard never called them, so no per-path exception is needed.

    13/18 -> 14/17 on 2026-10-06: finalize is implemented here (the status flip), calling the
    subscription API's ``trials/start`` for its second half.

    31 -> 32 on 2026-10-09: ``POST /xero/release`` joins the surface, proxied beside
    ``xero/disconnect`` (Flask owns the Xero token). It frees the organisation another
    company holds, for the move offered when a connect is refused. The wizard reached for it
    before this proxy existed and got a 404 that it showed as "I couldn't disconnect that
    company from Xero" -- which is exactly the per-path gap this test exists to catch.
    """
    assert len(PORTED) == 14
    assert len(PROXIED) == 18
    assert len(EXPECTED) == 32
    assert set(_operations()) == EXPECTED
