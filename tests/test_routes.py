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
}

#: Answered here, executed by Flask. Each touches a rail Flask owns -- Stripe, a Xero token,
#: subscription state, or a Flask-owned URL -- so duplicating it would mean two writers
#: against something that permits only one.
PROXIED = {
    # Group D2 -- Xero chart of accounts, Xero contacts, billing-backend's bill codes
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
    # Group G -- Stripe, trials, and the Xero disconnect
    "GET /payment-method",
    "POST /payment-method/setup",
    "POST /payment-method/complete",
    "GET /billing/payment-methods",
    "POST /billing/payment-methods/setup-intent",
    "POST /billing/payment-methods/confirm",
    "POST /billing/payment-methods/default",
    "GET /billing/accounts",
    "POST /billing/accounts",
    "POST /billing/authorize",
    "POST /finalize",
    "POST /xero/disconnect",
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
def test_every_proxied_route_goes_through_the_single_outbound_module(route):
    """``core/minty_client.py`` is the ONLY module allowed to call Flask.

    That is what makes the write rule checkable by reading one file. A handler that called
    ``requests`` directly would still work, and would quietly reopen the boundary -- so the
    check is on the import, not on the behaviour.
    """
    name = _operations().get(route)
    assert name, f"no handler found for {route}"
    source = importlib.import_module(name)
    assert hasattr(source, "minty_client"), (
        f"{name} serves a proxied route but does not import core.minty_client. "
        "Every outbound call to Flask must go through that one module."
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
    """
    assert len(PORTED) == 13
    assert len(PROXIED) == 20
    assert len(EXPECTED) == 33
    assert set(_operations()) == EXPECTED
