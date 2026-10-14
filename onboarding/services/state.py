"""Server-side onboarding resume state.

THE ``entities`` ROW IS THE SOURCE OF TRUTH, NOT THE BROWSER.

The wizard caches its progress in ``localStorage``, which is gone on a cold resume -- a new
browser, incognito, cleared storage, a different device. This module reconstructs the whole
picture from the database so resume works with no browser state at all. That is why this is
the endpoint most worth testing: it is the only one that reads across every domain the
wizard touches.

Ported from Minty's blueprints/entity/services/onboarding_state.py. Two departures, both
deliberate:

1. THE XERO RECONCILE GOES THROUGH FLASK. Flask calls Xero's /connections directly with a
   token it refreshed itself. This service asks Flask for a token first (see
   core/xero_tokens.py for why using a token is allowed where refreshing is not).

2. "COULD NOT VERIFY" IS NOT "DISCONNECTED". Flask's version returns early on any failure,
   which is correct, and the ported version keeps that -- but it is now explicit in the
   type: ``connected_tenant_ids`` returns None for "do not know" and an empty set for
   "Xero says the org is gone". Collapsing those would clear a live connection over a
   network blip.

A THIRD DEPARTURE WAS PLANNED AND THEN WITHDRAWN, which is worth recording so it is not
re-attempted: this response briefly carried a ``steps`` key -- the wizard's step table --
on the theory that the frontend was duplicating an ordering the backend already owned.
Reading the frontend showed the opposite. ``OnboardingApp.jsx`` documents that
``current_step`` here "is derived from a different ordering (modules -> Xero -> petty-cash
-> bills/invite) than the FE flow", deliberately does NOT use it as a landing step, and
records the bug that trusting it caused: "resume jumped straight to Connect to Accounting".
It uses ``current_step``/``max_reached`` only to raise the ceiling on already-unlocked
steps.

So the two orderings are not duplicates. They are different questions with different
answers, and the frontend's table additionally carries short/tiny label variants and
module-conditional grouping that this one never had. Publishing a step table from here
would assert a shared ordering that the frontend correctly refuses to share.
"""

import logging

from core import xero_tokens
from onboarding.services import invites as invites_service
from onboarding.services import sales_methods
from onboarding.services import steps as step_defs
from onboarding.services.plans import MODULE_BILL, MODULE_CODES, MODULE_PETTY_CASH
from shared_models.models import Entity, EntityPettycashSettings, EntitySetupSummary

logger = logging.getLogger("minty-onboarding")


def enabled_modules(setup) -> list[str]:
    """Module codes enabled for the entity, in ``MODULE_CODES`` order, from its
    ``entity_setup_summary`` row (None -> no modules).

    FAIL CLOSED: the view counts a module on only where its map row says ``is_enabled``; no
    row is OFF. Worth stating because Flask's version of this had drifted - it fell back to
    the catalog's ``is_active`` (default True), so a brand-new entity answered "both modules
    enabled" and ``derive_current_step`` skipped STEP_MODULE. The catalog's ``is_active``
    says whether a module is OFFERED, never who may use it.
    """
    if setup is None:
        return []
    on = {MODULE_PETTY_CASH: setup.pettycash_module_enabled, MODULE_BILL: setup.bill_module_enabled}
    return [code for code in MODULE_CODES if on.get(code)]


def setup_summary(entity_id) -> EntitySetupSummary | None:
    """The company's ``entity_setup_summary`` row (a Postgres view - see the model)."""
    return EntitySetupSummary.objects.filter(entity_id=entity_id).first()


def _reconcile_xero_disconnect(entity: Entity) -> None:
    """Detect a Xero-side revoke and clear local connection state.

    The wizard reads ``connected`` from ``entity.xero_org_id``, which lags reality if the
    user revoked the app from inside Xero rather than through our disconnect flow. This
    verifies against Xero's tenant list and flips the entity back to not-connected only on
    a CONFIRMED revoke: the token worked, and the tenant is absent from the answer.

    Best-effort by design. Any inconclusive result -- no connector, an unusable token, Xero
    unreachable, a non-200 -- leaves state untouched, so a transient failure never falsely
    drops a live connection. That asymmetry is the whole point: wrongly showing connected
    costs the user one confusing screen, wrongly showing disconnected costs them a
    reconnect they did not need.
    """
    if not entity.xero_org_id:
        return

    tenant_ids = xero_tokens.connected_tenant_ids(entity.id)
    if tenant_ids is None:
        # Could not verify. Not evidence of anything.
        return

    if str(entity.xero_org_id) in tenant_ids:
        return

    logger.info(
        "onboarding state: entity %s connection revoked on the Xero side; "
        "clearing local connection state",
        entity.id,
    )
    entity.status = "onboarding"
    entity.xero_org_id = None
    entity.connected_by_user_id = None
    entity.save(update_fields=["status", "xero_org_id", "connected_by_user_id"])


def _xero_state(setup) -> dict:
    """Xero connection picture for the wizard, from the setup row (read after
    ``_reconcile_xero_disconnect``, so a revoked connection already reads as gone)."""
    return {
        "connected": bool(setup and setup.xero_org_linked),
        "org": (setup.xero_tenant_name if setup else None) or "",
    }


def _account_codes_done(entity_id) -> bool:
    """True once the petty-cash account-code mapping has been saved (Step 6): the core
    ``pettycash_account_id`` is set on ``entity_pettycash_settings``."""
    return EntityPettycashSettings.objects.filter(
        entity_id=entity_id, pettycash_account_id__isnull=False
    ).exists()


def _sales_methods_state(entity_id: str) -> dict:
    """Saved petty-cash Sales Setting (Step 5) for resume rehydration.

    Shape mirrors the POST body of ``/api/onboarding/sales-methods`` so the frontend round
    trips with no translation. Empty lists when nothing has been saved.

    Reads ``entity_sale_setting`` -- the per-entity selection. Flask's docstring for this
    says it reads "sale_info rows", which is wrong and confusing, because there IS a
    ``sale_info`` catalog table and a ``sale_info_id`` column on this one.
    """
    # Delegates rather than repeating the query: this was a second copy of
    # sales_methods.list_grouped, and it had already drifted -- it hardcoded the type list
    # where the other used MANAGED_TYPES, so adding a managed type would have been picked up
    # by the endpoint and not by resume.
    #
    # The permission-checked entry point is not used here on purpose: /state is already past
    # its own membership gate, and SALES_METHOD_VIEW would refuse an invited cashier the
    # resume data they need.
    return sales_methods.grouped_methods(entity_id)


def _opening_balance_state(setup) -> dict | None:
    """Saved petty-cash opening balance (Step 5), or None if none has been saved.

    ``opening_balance`` and ``start_date`` from the setup row (they live on
    ``entity_pettycash_settings``). The starting cash is an opening balance, never an
    addition, so ``cash_addition`` is always 0 and ``adjusted_opening_balance`` equals the
    amount; all of them come back so the frontend can bind to either.

    The money column is ``numeric``; the wizard's JSON gets numbers, not strings.
    """
    if setup is None or setup.start_date is None:
        return None

    amount = float(setup.opening_balance) if setup.opening_balance is not None else None
    return {
        "opening_date": setup.start_date.isoformat(),
        "opening_balance": amount,
        "cash_addition": 0.0,
        "adjusted_opening_balance": amount,
    }


def derive_current_step(
    entity: Entity, modules: list[str], xero: dict, account_codes_done: bool
) -> int:
    """The furthest step the SAVED DATA justifies landing on.

    Conservative by design: only advance past a step once its data is present, so somebody
    who stopped after basic information lands on the module step -- the next thing to do --
    rather than deep inside a half-configured flow.

    A finalized entity (status != 'onboarding') reports the terminal step.
    """
    if entity.status != "onboarding":
        return step_defs.STEP_ALL_SET
    if not modules:
        return step_defs.STEP_MODULE
    if not xero.get("connected"):
        return step_defs.STEP_ACCOUNTING
    # Xero is connected. Petty-cash configuration is the next gate when that module is on.
    if MODULE_PETTY_CASH in modules and not account_codes_done:
        return step_defs.STEP_SALES
    if MODULE_BILL in modules:
        return step_defs.STEP_BILLS
    return step_defs.STEP_INVITE


def get_onboarding_state(user_id, entity: Entity) -> dict:
    """The full resume picture for ``entity``, sourced from the database.

    The caller has already established membership (core.permissions.entity_for_member), so
    this does no access checking of its own beyond the per-permission invite gate.
    """
    # The live Xero check may clear a revoked connection, so it runs before the one read of
    # the entity's setup row (entity_setup_summary) that the rest of the picture comes from.
    _reconcile_xero_disconnect(entity)
    setup = setup_summary(entity.id)
    modules = enabled_modules(setup)
    xero = _xero_state(setup)
    account_codes_done = _account_codes_done(entity.id)
    current_step = derive_current_step(entity, modules, xero, account_codes_done)

    return {
        "entity_id": entity.id,
        "status": entity.status,
        "current_step": current_step,
        "max_reached": current_step,
        # The step the user last "Saved and Exited" on, returned VERBATIM as the frontend
        # step id -- not remapped onto current_step's ordering. null if never set.
        "saved_step": entity.onboarding_saved_step,
        "entity": {
            "name": entity.name or "",
            # country: ISO alpha-2 (country_info PK). currency: uuid into currency_info.
            # str() on the uuid -- Django hands back a UUID object where Flask hands back
            # a string, and the wizard submits this value straight back.
            "country": entity.country_code or "",
            "currency": str(entity.currency_id) if entity.currency_id else "",
            # Optional Step 1 contact details. ALWAYS PRESENT, as "" when unset, so the
            # wizard can tell "the server has no value" from "the server didn't send the
            # field" -- its rehydration keys off presence, and an empty value is legitimate.
            "phone": entity.contact_phone or "",
            "email": entity.business_email or "",
        },
        "modules": modules,
        "xero": xero,
        "sales_methods": _sales_methods_state(entity.id),
        "opening_balance": _opening_balance_state(setup),
        # Best-effort: a permission gap must not fail the whole resume read. See
        # services/invites.py::list_pending_best_effort.
        "invites": invites_service.list_pending_best_effort(user_id, entity.id),
    }
