"""Group A -- reference data. The first group to cut over.

Four endpoints, no writes, and between them they exercise the whole stack: the database
connection and its search_path, the model mirrors, bearer auth, CORS, and the error
shape. That is why this group goes first -- if these four answer correctly from the
wizard's browser, everything structural is proven and the remaining groups are business
logic rather than plumbing.

AUTH IS NOT UNIFORM HERE, AND THAT IS PORTED, NOT INVENTED

Flask leaves three of these four public and gates only ``/plans``:

  * ``server-time``, ``currencies``, ``countries`` are public. They are reference data --
    today's date, and two registries seeded from ISO standards. Nothing about them is
    per-user, and the wizard needs the registries to render Step 1 before it has
    necessarily settled a token.
  * ``/plans`` is token-gated. Not because prices are secret -- they are on the pricing
    page -- but so an unauthenticated caller cannot drive load against the catalog. The
    Flask docstring gives that reason explicitly.

Keeping the same split matters: making the registries require a token would break Step 1
for a cold resume, and gating nothing would widen a surface Flask deliberately narrowed.

THE UUID TRAP

``currency_info.id`` is a Postgres uuid. Flask declares it ``UUID(as_uuid=False)``, so
SQLAlchemy hands back a ``str`` and its JSON carries a plain string. Django's
``UUIDField`` hands back a ``uuid.UUID``, which ninja would serialise differently -- and
the wizard submits whatever it was given straight back as ``currency_id``. Every uuid
crossing this boundary is ``str()``-ed on the way out.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from ninja import Router, Status

from core.auth import OnboardingBearerAuth
from shared_models.models import CountryInfo, CurrencyInfo

from onboarding.services import plans as plans_service

#: Public: reference data with nothing per-user in it. See the module header.
public_router = Router(auth=None)

#: Token-gated: the price catalog. Not secret, but not an open load generator either.
plans_router = Router(auth=OnboardingBearerAuth())


@public_router.get("/server-time")
def server_time(request):
    """Server-authoritative "today", so the date picker caps at the user's midnight.

    The wizard's date fields must not allow a future date, and "future" has to mean
    future where the USER is -- a browser in another timezone would otherwise disagree
    with the server about what day it is and let a tomorrow through.

    Asia/Hong_Kong, matching Minty's hardcoded tz (models/db.py, bootstrap.py). This is
    the one display-timezone decision in the service; storage stays UTC.
    """
    today = datetime.now(ZoneInfo(settings.DISPLAY_TIMEZONE)).date()
    return {"today": today.isoformat()}


@public_router.get("/currencies")
def currencies(request):
    """Currency registry for the Step 1 dropdown, ordered by name.

    The dropdown SHOWS ``currency_name`` but SUBMITS ``currency_id`` -- the uuid PK --
    so the created entity's ``currency_id`` FK gets a uuid rather than a code.

    The response keys keep their historical names: ``iso_code`` carries
    ``currency_info.currency_code``. Renaming it would be tidier and would break the
    wizard, so it stays.

    Only ``is_active`` rows are offered. The table is seeded with the full ISO 4217 list
    (~170 rows); ``is_active`` narrows that to what this deployment actually operates in.
    No preselect concern here -- this is the new-entity wizard, so there is no existing
    value to preserve (unlike the settings dropdowns, which keep the entity's current row
    whatever its flag says).
    """
    rows = (
        CurrencyInfo.objects.filter(is_active=True)
        .order_by("currency_name")
        .values_list("id", "currency_name", "currency_code")
    )
    return {
        "currencies": [
            # str(): see the uuid trap in the module header.
            {"currency_id": str(cid), "currency_name": name, "iso_code": code}
            for cid, name, code in rows
        ]
    }


@public_router.get("/countries")
def countries(request):
    """Country registry for the Step 1 dropdown.

    ``country_info``'s PK is the ISO alpha-2 code, so ``country_id`` carries that code
    too. The duplicate key is kept because the wizard's submit-the-id contract needs no
    change -- create and update both resolve codes.

    Ordered by ``display_order`` then name, so common countries can be floated above the
    alphabetical tail by setting a value below the 999 default. Ties fall back to
    alphabetical, which is what every row does while the default stands.
    """
    rows = (
        CountryInfo.objects.filter(is_active=True)
        .order_by("display_order", "country_name_en")
        .values_list("country_code", "country_name_en")
    )
    return {
        "countries": [
            {"country_id": code, "country_name_en": name, "country_code": code}
            for code, name in rows
        ]
    }


# 503 must be declared or ninja turns it into a 500 -- see api_entity.create_entity.
@plans_router.get("/plans", response={200: dict, 503: dict})
def plans(request):
    """Module price list for Step 2's subscription summary.

    Read from ``billing_plan``, not from Stripe -- see onboarding/services/plans.py for
    why the Flask docstring saying otherwise is stale.

    A failure here answers 503 and the wizard hides the summary rather than blocking
    module selection. That posture is ported deliberately: a customer must be able to
    pick their modules even when the price panel cannot render, because the trial is
    card-free and nothing is being charged at this step.
    """
    if not settings.SUBSCRIPTION_ENABLED:
        # Subscriptions dark (config.settings): nothing to quote. An empty list is what the
        # wizard already treats as "no summary"; the flag lets it hide the billing sheet.
        return {"plans": [], "subscriptions_enabled": False}
    try:
        data = plans_service.get_module_plan_catalog()
        data["subscriptions_enabled"] = True
        return data
    except Exception:
        # Logged with a traceback, answered blandly. Ported from Flask, which does the
        # same rather than letting the generic 500 handler take it -- 503 tells the
        # wizard "try again later", which is true, where 500 reads as "this is broken".
        import logging

        logging.getLogger("minty-onboarding").exception(
            "plans: could not load the plan catalog"
        )
        # Status(), not a tuple -- see api_entity.create_entity. This path had no test
        # reaching it, so the bare tuple would have surfaced as a 500 the first time
        # the catalog actually failed: the exact moment a clear answer matters most.
        return Status(503, {"error": "Plans are unavailable right now."})
