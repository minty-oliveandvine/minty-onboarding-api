"""Group G -- money, finalize, and the one Xero call that revokes a connection.

WHO OWNS WHAT:

  * ``payment-method`` and ``billing/*`` create Stripe customers, SetupIntents and payment
    methods, and record billing consent. Cards are captured in-app with Stripe Elements and
    always land on a billing account. The one writer of Stripe and the subscription tables is
    minty-subscription-api, so these are proxied there (``core/subscription_client.py``).
  * ``finalize`` is implemented HERE: it flips the company live (this service's own write on
    ``entities``, as ``/create`` is) and then asks the subscription API to start the card-free
    trials. A failed trial start FAILS finalize -- a company must never end up live with the
    trial it was promised silently missing -- and the All Set screen offers Try again. Both
    halves are idempotent, so the retry is safe.
  * ``xero/disconnect`` calls Xero's DELETE /connections and clears the local token state;
    Flask owns the Xero token, so it is proxied there (``core/minty_client.py``).

WHY PROXY RATHER THAN LET THE WIZARD CALL THOSE SERVICES DIRECTLY

So the frontend ends up with ONE base URL. ``lib/apiRoutes.ts`` maps paths to services, and a
path that has to bypass it is a path someone has to remember.

Each proxy forwards the caller's own bearer token, so the receiving service applies exactly the
membership and consent checks it would have applied to a direct call. This service adds no
privilege of its own -- there is no service credential here to scope wrongly.
"""

import logging

from ninja import Body, Router

from core import minty_client, subscription_client
from core.exceptions import UpstreamError
from core.permissions import entity_for_member, require_entity_id
from shared_models.enums import EntityStatus

logger = logging.getLogger("minty-onboarding")

billing_router = Router()


def _get(request, path: str, entity_id: str):
    entity_id = require_entity_id(entity_id)
    return subscription_client.proxy(
        request, path, method="GET", params={"entity_id": entity_id}
    )


# --- Legacy per-entity payment method -------------------------------------------------
@billing_router.get("/payment-method")
def get_payment_method(request, entity_id: str = ""):
    """``{has_payment_method, has_billing_consent}`` for the entity."""
    return _get(request, "/api/onboarding/payment-method", entity_id)


# --- The payer's card shelf -----------------------------------------------------------
#
# The subscription API serves these under /api/onboarding/* beside the payer portal's
# /api/me/billing/payment-methods*, sharing service code underneath.
@billing_router.get("/billing/payment-methods")
def get_billing_payment_methods(request):
    return subscription_client.proxy(
        request, "/api/onboarding/billing/payment-methods", method="GET"
    )


@billing_router.post("/billing/payment-methods/setup-intent")
def post_setup_intent(request, payload: dict = Body(default={})):
    """Returns a Stripe SetupIntent AND the publishable key.

    The key comes from the subscription API rather than from a wizard build-time env var on
    purpose: one service owns the Stripe account, so the browser cannot end up talking to a
    different account than the backend does.
    """
    return subscription_client.proxy(
        request, "/api/onboarding/billing/payment-methods/setup-intent", json=payload
    )


@billing_router.post("/billing/payment-methods/confirm")
def post_confirm_payment_method(request, payload: dict = Body(default={})):
    """Adopts a confirmed card: creates the Stripe customer and opens the billing account."""
    return subscription_client.proxy(
        request, "/api/onboarding/billing/payment-methods/confirm", json=payload
    )


@billing_router.post("/billing/payment-methods/default")
def post_default_payment_method(request, payload: dict = Body(default={})):
    """Kept for completeness; the wizard deliberately no longer calls it."""
    return subscription_client.proxy(
        request, "/api/onboarding/billing/payment-methods/default", json=payload
    )


@billing_router.get("/billing/accounts")
def get_billing_accounts(request):
    return subscription_client.proxy(request, "/api/onboarding/billing/accounts", method="GET")


@billing_router.post("/billing/accounts")
def post_billing_accounts(request, payload: dict = Body(default={})):
    return subscription_client.proxy(request, "/api/onboarding/billing/accounts", json=payload)


@billing_router.post("/billing/authorize")
def post_billing_authorize(request, payload: dict = Body(default={})):
    """Records per-entity billing consent -- who agreed that this card pays for this company."""
    return subscription_client.proxy(request, "/api/onboarding/billing/authorize", json=payload)


# --- Finishing, and disconnecting -----------------------------------------------------
@billing_router.post("/finalize")
def post_finalize(request, payload: dict = Body(default={})):
    """Body ``{entity_id}`` -> ``{"status": "success", "trial_end": iso8601 | null}``.

    Called on ARRIVAL at the All Set step. Two halves, both idempotent:

    1. A company still ``onboarding`` goes live: ``connected`` when a Xero org is linked,
       else ``disconnected`` (``entity_status`` has no ``active``). Already live -> untouched.
    2. ``POST /api/onboarding/trials/start`` on the subscription API starts the card-free
       trials for the modules the wizard enabled and reads ``trial_end`` back from the rows.
       Called on every finalize, not only the first: a retry after a failed start must reach
       it, and modules that already hold a trial are skipped there.

    A failed trial start FAILS finalize with the subscription API's own status and sentence.
    The company stays live (half 1 is not undone) and Try again redoes half 2.
    """
    entity = entity_for_member(request.auth_user_id, (payload or {}).get("entity_id"))

    if entity.status == EntityStatus.ONBOARDING:
        entity.status = (
            EntityStatus.CONNECTED if entity.xero_org_id else EntityStatus.DISCONNECTED
        )
        entity.save(update_fields=["status"])
        logger.info("onboarding: entity %s finalized (%s)", entity.id, entity.status)

    answer, status = subscription_client.forward(
        request, "/api/onboarding/trials/start", json={"entity_id": str(entity.id)}
    )
    if status != 200:
        message = answer.get("error") if isinstance(answer, dict) else None
        logger.error(
            "onboarding: finalize of entity %s failed to start trials (%s)", entity.id, status
        )
        raise UpstreamError(message or minty_client.UNREACHABLE, status=status)

    return {"status": "success", "trial_end": answer.get("trial_end")}


@billing_router.post("/xero/disconnect")
def post_xero_disconnect(request, payload: dict = Body(default={})):
    """Revokes the Xero connection at Xero and clears the local token state."""
    return minty_client.proxy(request, "/api/onboarding/xero/disconnect", json=payload)


@billing_router.post("/xero/release")
def post_xero_release(request, payload: dict = Body(default={})):
    """Frees the Xero organisation held by ANOTHER company, so this one can connect it.

    The wizard's half of the move offered when a connect is refused ("one organisation, one
    company"). ``entity_id`` in the body is the company being FREED, never the one being
    onboarded; Flask authorizes it on that company and leaves it ``disconnected``. Proxied for
    the same reason as ``xero/disconnect``: Flask owns the Xero token.
    """
    return minty_client.proxy(request, "/api/onboarding/xero/release", json=payload)
