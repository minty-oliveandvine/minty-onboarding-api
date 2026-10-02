"""URL mounting.

THE PATHS MATCH FLASK BYTE FOR BYTE.

Every route lands on ``/api/onboarding/<name>`` -- exactly where Flask serves it today.
That is the single decision that makes the cutover cheap: the frontend swaps a base URL
per endpoint group and rewrites no paths, so moving a group is one line in
``lib/apiRoutes.js`` and moving it back is the same line. A tidier scheme
(``/api/v1/onboarding/...``, say) would buy nothing and cost a path-rewriting layer on
both sides.

AUTH DEFAULTS TO ON.

``NinjaAPI(auth=...)`` makes every endpoint token-gated unless its router says otherwise,
so forgetting the decorator on a new endpoint fails closed. The one public router opts
out explicitly, and api_reference.py argues each of its three endpoints.
"""

from django.http import JsonResponse
from django.urls import path
from ninja import NinjaAPI

from core.auth import OnboardingBearerAuth
from core.exceptions import register_exception_handlers

api = NinjaAPI(
    title="Minty Onboarding API",
    version="1.0.0",
    description="The onboarding wizard's backend, extracted from Minty (Module 1).",
    auth=OnboardingBearerAuth(),
    # Flask's surface has no /api/onboarding/docs, and this service must not invent a
    # path there while both answer the same prefix through a frontend switch.
    docs_url="/_docs",
)

register_exception_handlers(api)

from onboarding.api_reference import plans_router, public_router  # noqa: E402
from onboarding.api_entity import entity_router  # noqa: E402
from onboarding.api_billing import billing_router  # noqa: E402
from onboarding.api_invites import invites_router  # noqa: E402
from onboarding.api_modules import modules_router  # noqa: E402
from onboarding.api_pettycash import pettycash_router  # noqa: E402
from onboarding.api_state import state_router  # noqa: E402

# Group A -- reference data. Mounted at the root of the prefix so paths stay flat,
# matching Flask, which registers every onboarding route with no url_prefix.
api.add_router("/", public_router, tags=["Reference"])
api.add_router("/", plans_router, tags=["Reference"])

# Group B -- wizard state.
api.add_router("/", state_router, tags=["Wizard state"])

# Group C -- the company itself.
api.add_router("/", entity_router, tags=["Entity"])

# Group D -- petty-cash config. D1 (sales methods, opening balance) is ported; the four
# Xero/bills endpoints inside it are proxied to Flask. See onboarding/api_pettycash.py.
api.add_router("/", pettycash_router, tags=["Petty cash"])

# Group E -- invitations. List and cancel are ported; sending is proxied because the
# invite email links into a Flask route. See onboarding/api_invites.py.
api.add_router("/", invites_router, tags=["Invites"])

# Group F -- module selection. Proxied: enabling a module is a subscription write.
api.add_router("/", modules_router, tags=["Modules"])

# Group G -- money, and the Xero disconnect. Proxied, every one.
api.add_router("/", billing_router, tags=["Billing"])

# Every /api/onboarding/* path Flask serves is now answered here -- ported or proxied -- so
# the wizard points at ONE base URL. Which endpoints are genuinely local is recorded in
# tests/test_routes.py and in the README group table.


def health(request):
    """Liveness only -- does not touch the database.

    Deliberately not a readiness check. The container entrypoint already waits for the
    database and the pettycashv3 schema before starting, so a health endpoint that also
    queried would report unhealthy for a transient database blip and get the container
    killed mid-request. If a readiness probe is wanted later it should be its own path.
    """
    return JsonResponse({"status": "ok", "service": "minty-onboarding-api"})


urlpatterns = [
    path("health", health, name="health"),
    path("api/onboarding/", api.urls),
]
