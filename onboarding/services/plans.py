"""The module price catalog behind GET /api/onboarding/plans.

READ FROM THE DATABASE, NOT FROM STRIPE.

The Flask docstring this ports says the prices come "live from Stripe". That is stale and
worth correcting rather than copying: ``subscription/services/catalog.py`` explicitly
"replaces the half of stripe_state that read Stripe Products and Prices", imports no
stripe module, and reads ``billing_plan``. The chain is
``get_module_plan_catalog -> catalog.available_plans -> store.active_billing_plans ->
BillingPlan.query``. Nothing leaves the process.

That matters for the boundary: if this endpoint touched Stripe it would have to be
proxied to Flask under the write rule. It does not, so it lives here.

HOW A PLAN IS KEYED

``billing_plan.code`` is the SET of modules a plan bills, upper-cased, sorted, joined
with '+':

    'PETTY_CASH'          one module
    'BILL'                one module
    'BILL+PETTY_CASH'     the bundle

A code containing '+' is a bundle and is deliberately excluded from the ``plans`` list: a
bundle is not something a customer picks off a shelf, it is what two modules cost
together. It comes back separately as ``bundle_amount`` / ``bundle_codes``, and the
wizard bills the bundle price when the picked set is exactly those codes, else the sum of
the standalone plans. THE BUNDLE IS THE DISCOUNT -- there is no separate discount field
and inventing one would double-count.

A module with no live plan row is OMITTED rather than priced at zero, so a half-seeded
catalog cannot invent a free module.
"""

import logging

from shared_models.models import BillingPlan, BillingPolicy, EntityFunction

from onboarding.services import money
from shared_models.enums import ModuleCode

logger = logging.getLogger("minty-onboarding")

#: Canonical module codes, in display order: the ``module_code`` enum
#: (shared_models/enums.py, schema item 20). ``MODULE_BILL`` keeps its historical name.
MODULE_PETTY_CASH = ModuleCode.PETTY_CASH.value
MODULE_BILL = ModuleCode.PAYMENT_REQUEST.value
MODULE_CODES = (MODULE_PETTY_CASH, MODULE_BILL)

#: ``billing_plan.code`` keeps the word BILL for the Payment Request module by decision;
#: the module code is PAYMENT_REQUEST. Mapped here, in one place (Flask:
#: blueprints/subscription/services/billing.plan_code / plan_modules).
PLAN_WORD_BY_MODULE = {"PAYMENT_REQUEST": "BILL"}
MODULE_BY_PLAN_WORD = {v: k for k, v in PLAN_WORD_BY_MODULE.items()}


def plan_modules(code: str) -> list[str]:
    """Module codes a ``billing_plan.code`` bills, sorted: 'BILL+PETTY_CASH' ->
    ['PAYMENT_REQUEST', 'PETTY_CASH']."""
    return sorted(
        MODULE_BY_PLAN_WORD.get(w, w)
        for w in ((part or "").strip().upper() for part in (code or "").split("+"))
        if w
    )

#: Shipped default if ``billing_policy`` has no row -- an app running before the
#: migration, or a test database where it was never seeded. Quiet, because the default
#: IS the shipped behaviour. Matches policy.DEFAULT_TRIAL_DAYS in Flask.
DEFAULT_TRIAL_DAYS = 30


def _interval(plan: BillingPlan) -> tuple[str, int]:
    """``billing_plan`` stores months; the response speaks Stripe's vocabulary.

    Kept as a translation rather than a stored string so a future yearly plan is a
    number in the table, not a new column.
    """
    months = int(plan.interval_months or 1)
    if months % 12 == 0:
        return "year", months // 12
    return "month", months


def _active_plans() -> list[BillingPlan]:
    """Every sellable plan, cheapest first. Same ordering as Flask's store."""
    return list(
        BillingPlan.objects.filter(is_active=True).order_by("amount", "code")
    )


def _trial_days() -> int:
    """``billing_policy.trial_days``, or the shipped default.

    Never raises: the policy row is a tunable, and a page that cannot state the trial
    length is worse than one stating the default. Flask's policy loader takes the same
    posture for the same reason.
    """
    try:
        row = BillingPolicy.objects.filter(id=1).first()
    except Exception:  # noqa: BLE001 - policy must not break the page
        logger.exception("plans: could not read billing_policy; using shipped default")
        return DEFAULT_TRIAL_DAYS
    if row is None or row.trial_days is None:
        logger.debug("plans: no billing_policy row; using shipped default")
        return DEFAULT_TRIAL_DAYS
    days = int(row.trial_days)
    return days if days >= 0 else DEFAULT_TRIAL_DAYS


def get_module_plan_catalog() -> dict:
    """The price list for the wizard's Step 2 summary.

    Entity-independent: no customer, no subscriptions, just what each module costs and
    what the bundle costs. Amounts come back as JSON numbers already converted out of
    minor units, plus a pre-formatted string so the frontend never has to know a
    currency's decimal places.
    """
    currencies = money.load_currencies()

    all_plans = _active_plans()
    singles = {
        plan_modules(p.code)[0]: p
        for p in all_plans
        if "+" not in (p.code or "") and plan_modules(p.code)
    }

    names = {
        fn.function_code: fn.function_name
        for fn in EntityFunction.objects.filter(function_code__in=MODULE_CODES)
    }

    plans = []
    for code in MODULE_CODES:
        plan = singles.get(code.upper())
        if plan is None:
            # No sellable row for this module. Omit it -- see the module header.
            continue
        currency = (plan.currency or "").upper()
        interval, _count = _interval(plan)
        plans.append(
            {
                "code": code,
                "name": names.get(code) or code,
                "amount": float(money.to_major(currencies, plan.amount, currency)),
                "formatted_amount": money.format_minor(
                    currencies, plan.amount, currency
                ),
                "currency_code": currency,
                "currency_symbol": money.symbol(currencies, currency),
                "billing_interval": interval or "month",
            }
        )

    bundle = _bundle_plan(all_plans)
    if bundle is None:
        bundle_amount = 0.0
        bundle_codes: list[str] = []
        bundle_currency = None
    else:
        bundle_currency = (bundle.currency or "").upper()
        bundle_amount = float(
            money.to_major(currencies, bundle.amount, bundle_currency)
        )
        bundle_codes = plan_modules(bundle.code)
        bundle_currency = bundle_currency or None

    return {
        "plans": plans,
        "bundle_amount": bundle_amount,
        "bundle_codes": bundle_codes,
        "bundle_currency": bundle_currency,
        "trial_period_days": _trial_days(),
    }


def _bundle_plan(all_plans: list[BillingPlan]) -> BillingPlan | None:
    """The multi-module plan, or None if the catalog has none.

    Takes the LARGEST multi-code plan so a future three-module bundle wins over a
    two-module one, rather than the answer depending on row order.
    """
    multi = [p for p in all_plans if "+" in (p.code or "")]
    if not multi:
        return None
    return max(multi, key=lambda p: len((p.code or "").split("+")))
