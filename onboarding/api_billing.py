"""Group G -- money, and the one Xero call that revokes a connection. Entirely proxied.

EVERY ENDPOINT HERE TOUCHES A RAIL FLASK OWNS:

  * ``payment-method`` and ``billing/*`` create Stripe customers, SetupIntents and payment
    methods, and record billing consent. Cards are captured in-app with Stripe Elements and
    always land on a billing account; there is no hosted Checkout route any more. Flask's ``subscription/services/checkout.py`` is
    ~3,200 lines of trial, proration and dunning logic treating the local tables as source of
    truth, with no webhook receiver to reconcile a second writer. A second writer there does
    not cause a merge conflict; it charges somebody twice.
  * ``finalize`` flips the company to active AND starts the card-free trials. Starting a
    trial is the moment a module legitimately becomes enabled -- the very thing Group F is
    not allowed to do directly.
  * ``xero/disconnect`` calls Xero's DELETE /connections and clears the local token state.

WHY PROXY RATHER THAN LET THE WIZARD CALL FLASK DIRECTLY

So the frontend ends up with ONE base URL. ``lib/apiRoutes.js`` maps paths to services, and a
path that has to bypass it is a path someone has to remember. It also means these endpoints
move to ``subscription-service`` later by changing one line here rather than in the wizard.

The proxy forwards the caller's own bearer token, so Flask applies exactly the membership and
consent checks it would have applied to a direct call. This service adds no privilege of its
own -- there is no service credential here to scope wrongly.
"""

from ninja import Body, Router

from core import minty_client
from core.permissions import require_entity_id

billing_router = Router()


def _get(request, path: str, entity_id: str):
    entity_id = require_entity_id(entity_id)
    return minty_client.proxy(
        request, path, method="GET", params={"entity_id": entity_id}
    )


# --- Legacy per-entity payment method -------------------------------------------------
@billing_router.get("/payment-method")
def get_payment_method(request, entity_id: str = ""):
    """``{has_payment_method, has_billing_consent}`` for the entity."""
    return _get(request, "/api/onboarding/payment-method", entity_id)


# --- The payer's card shelf -----------------------------------------------------------
#
# These are DELIBERATE DUPLICATES of the payer portal's /api/me/billing/payment-methods*,
# sharing service code underneath. They exist only because the portal's routes hard-code
# Access-Control-Allow-Origin to PAYMENT_REQUEST_WEB_URL, and onboarding is a different origin.
# The wizard's own lib/billing.js carries the warning: do not "simplify" these to the
# /api/me routes.
#
# Django's multi-origin CORS makes that duplication removable -- but not while these stay on
# the Flask side, so it is a cleanup for whoever moves Group G.
@billing_router.get("/billing/payment-methods")
def get_billing_payment_methods(request):
    return minty_client.proxy(
        request, "/api/onboarding/billing/payment-methods", method="GET"
    )


@billing_router.post("/billing/payment-methods/setup-intent")
def post_setup_intent(request, payload: dict = Body(default={})):
    """Returns a Stripe SetupIntent AND the publishable key.

    The key comes from Flask rather than from a wizard build-time env var on purpose: one service
    owns the Stripe account, so the browser cannot end up talking to a different account than
    the backend does.
    """
    return minty_client.proxy(
        request, "/api/onboarding/billing/payment-methods/setup-intent", json=payload
    )


@billing_router.post("/billing/payment-methods/confirm")
def post_confirm_payment_method(request, payload: dict = Body(default={})):
    """Adopts a confirmed card: creates the Stripe customer and opens the billing account."""
    return minty_client.proxy(
        request, "/api/onboarding/billing/payment-methods/confirm", json=payload
    )


@billing_router.post("/billing/payment-methods/default")
def post_default_payment_method(request, payload: dict = Body(default={})):
    """Kept for completeness; the wizard deliberately no longer calls it."""
    return minty_client.proxy(
        request, "/api/onboarding/billing/payment-methods/default", json=payload
    )


@billing_router.get("/billing/accounts")
def get_billing_accounts(request):
    return minty_client.proxy(request, "/api/onboarding/billing/accounts", method="GET")


@billing_router.post("/billing/accounts")
def post_billing_accounts(request, payload: dict = Body(default={})):
    return minty_client.proxy(request, "/api/onboarding/billing/accounts", json=payload)


@billing_router.post("/billing/authorize")
def post_billing_authorize(request, payload: dict = Body(default={})):
    """Records per-entity billing consent -- who agreed that this card pays for this company."""
    return minty_client.proxy(request, "/api/onboarding/billing/authorize", json=payload)


# --- Finishing, and disconnecting -----------------------------------------------------
@billing_router.post("/finalize")
def post_finalize(request, payload: dict = Body(default={})):
    """Flips the company to active and starts the card-free trials.

    The trial start is what legitimately enables a module -- so this is the endpoint Group F
    defers to, not merely another Stripe call.
    """
    return minty_client.proxy(request, "/api/onboarding/finalize", json=payload)


@billing_router.post("/xero/disconnect")
def post_xero_disconnect(request, payload: dict = Body(default={})):
    """Revokes the Xero connection at Xero and clears the local token state."""
    return minty_client.proxy(request, "/api/onboarding/xero/disconnect", json=payload)
