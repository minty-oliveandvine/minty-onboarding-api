"""Group F -- module selection. Entirely proxied, and the reason is the whole write rule.

``POST /modules`` looks like the most obviously "entity" write in the wizard: the user ticks
which modules they want, and the map table records it. It is not.

``entity_function_map.is_enabled`` is not a fact of its own. billing-backend's
``core/entitlements.py`` states it plainly -- it is "a projection of
``entity_module_subscription``, which the subscription lifecycle writes". A module is granted
when a trial or a subscription starts, never because a wizard step said so. Turning one on
here would set the projection without the thing it projects, and the two would then disagree
about what the customer is entitled to -- Minty denying a module the entitlements endpoint
granted, or the reverse. That exact disagreement has happened before, which is why the
resolver on both sides now fails closed.

So this is a subscription write wearing an entity-shaped hat, and it stays in Flask until
``subscription-service`` exists. This service's own write to that table is limited to seeding
all-disabled rows at entity creation, guarded so it cannot write True -- see
``onboarding/services/entity_create.py``.
"""

from ninja import Body, Router

from core import minty_client

modules_router = Router()


@modules_router.post("/modules")
def post_modules(request, payload: dict = Body(default={})):
    return minty_client.proxy(request, "/api/onboarding/modules", json=payload)
