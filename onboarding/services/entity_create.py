"""Creating a company. The first thing in this service that writes.

Ported from Minty's ``blueprints/entity/services/shared.py::create_entity_for_user``, which
is shared between the Jinja entity-create form and the onboarding API. Only the onboarding
half is ported; the form keeps using Flask's copy, so BOTH paths write ``entities`` during
the cutover. They are safe together because the rows are disjoint per request -- but never
let both run for a single company.

FOUR TABLES, NOT ONE

Creating an entity writes:

    entities              the company
    user_entity           the creator's membership
    entity_sale_setting   default payment/delivery methods
    entity_function_map   one row per module, ALL OFF  <- see the guard below

IDEMPOTENCY IS LOAD-BEARING, NOT A NICETY

A cold resume has no ``localStorage``, so a stale or racing wizard can re-POST Step 1 for a
company this user already created and abandoned mid-onboarding. Creating a second row would
give them two half-finished companies with the same name and no way to tell which one the
wizard is bound to. So an existing onboarding entity with the same name and the same member
is RETURNED rather than duplicated.

THE MODULE SEED, AND THE GUARD ON IT

``entity_function_map`` is otherwise read-only to this service, because ``is_enabled`` is a
projection of subscription state. The seed is the one exception, and it is allowed because
it writes only ``False``:

  * it grants nothing;
  * it exists to CLOSE a permissive hole. With no explicit row the resolver fell back to
    the catalog's ``is_active`` (default True), and Flask's comment records the cost --
    "every entity ever created kept Petty Cash for free while its card still offered Start
    free trial";
  * Flask's ``create.py`` claims the seed is "Petty Cash enabled, Bill disabled". That
    comment is stale. ``DEFAULT_MODULE_STATE`` is both modules ``False``, and the note above
    it explains the change. Do not restore the old behaviour from the comment.

:func:`_seed_module_defaults` raises on any attempt to write ``True``, so the narrowing is
enforced by the code rather than remembered by a reader.
"""

import logging
import uuid
from datetime import datetime, timezone

from django.db import transaction

from core.exceptions import ConflictError, OnboardingValidationError
from shared_models.models import (Entity, EntityFunction, EntityFunctionMap,
                                  EntitySaleSetting, SaleInfo, UserEntity)

# Imported rather than redeclared: two lists each calling themselves canonical is exactly
# how they drift apart. services/plans.py owns the module codes; state.py imports them from
# there too, so this is now the single definition.
from onboarding.services.plans import MODULE_CODES
from shared_models.enums import SaleType

logger = logging.getLogger("minty-onboarding")

#: Every module OFF at creation. Creation grants nothing -- a module switches on when its
#: trial or subscription starts. See the module header before changing this.
DEFAULT_MODULE_STATE: dict[str, bool] = {code: False for code in MODULE_CODES}

#: The creator's role. ADMIN, not super_admin -- ported verbatim from Flask's
#: "The creator becomes the entity admin by policy". admin (rank 4) already clears every
#: gate the wizard applies, so promoting them would widen standing for no requirement.
CREATOR_ROLE = "admin"


#: The methods a brand-new company starts with, and their order. The catalogue itself is
#: global; a default that is not in it yet (a fresh database) is added. Cash leads and is
#: type ``other`` keyed ``cash_sales``: the closing-balance figure is found by that key.
DEFAULT_SALES_METHODS = (
    # (name, value_name, type, order)
    ("Cash", "cash_sales", SaleType.OTHER, 0),
    ("Visa", "visa_sales", SaleType.ELECTRONIC, 1),
    ("Alipay", "alipay_sales", SaleType.ELECTRONIC, 2),
    ("WeChat Pay", "wechat_sales", SaleType.ELECTRONIC, 3),
    ("Mastercard", "master_sales", SaleType.ELECTRONIC, 4),
    ("UnionPay", "unionpay_sales", SaleType.ELECTRONIC, 5),
    ("Amex", "amex_sales", SaleType.ELECTRONIC, 6),
    ("Octopus", "octopus_sales", SaleType.ELECTRONIC, 7),
    ("Food Panda", "foodpanda_sales", SaleType.DELIVERY, 1),
    ("Keeta", "keeta_sales", SaleType.DELIVERY, 2),
    ("OpenRice", "openrice_sales", SaleType.DELIVERY, 3),
)


def _seed_default_sales_methods(entity_id: str) -> None:
    """Link a new company to the default sales methods (same list as Flask's
    ``create_default_entity_settings``). Idempotent per (entity, catalogue row)."""
    from onboarding.services.sales_methods import ensure_catalog_row

    existing = set(
        EntitySaleSetting.objects.filter(entity_id=entity_id).values_list("sale_id", flat=True)
    )
    rows = []
    for name, value_name, sale_type, order in DEFAULT_SALES_METHODS:
        catalog = SaleInfo.objects.filter(value_name=value_name).first() or ensure_catalog_row(
            name, sale_type, value_name=value_name, display_order=order
        )
        if catalog.id in existing:
            continue
        existing.add(catalog.id)
        rows.append(
            EntitySaleSetting(entity_id=entity_id, sale=catalog, is_active=True, display_order=order)
        )
    if rows:
        EntitySaleSetting.objects.bulk_create(rows)
    logger.info("onboarding: linked %s default sales methods for entity %s", len(rows), entity_id)


def _seed_module_defaults(entity_id: str, state: dict[str, bool] | None = None,
                          user_id=None) -> None:
    """Write one ``entity_function_map`` row per module, all disabled.

    THE GUARD: this refuses to write ``is_enabled=True``. Enabling a module is a
    subscription write and belongs to Flask until ``subscription-service`` exists (see the
    module header and shared_models/models.py). Raising rather than silently coercing, so a
    future caller that tries fails loudly in tests instead of quietly granting a paid module
    for free.

    Idempotent: an entity that already has a row for a module is left alone, so a retried
    create cannot produce duplicate or contradictory rows -- and, importantly, cannot
    overwrite a module the subscription lifecycle has since switched ON.
    """
    state = dict(DEFAULT_MODULE_STATE if state is None else state)

    granted = [code for code, enabled in state.items() if enabled]
    if granted:
        raise AssertionError(
            "onboarding-backend may not enable a module: "
            f"{granted}. entity_function_map.is_enabled is a projection of "
            "entity_module_subscription, which the subscription lifecycle writes. "
            "Route the grant through Flask instead."
        )

    catalog = {
        fn.function_code: fn.id
        for fn in EntityFunction.objects.filter(function_code__in=list(state))
    }
    missing = [code for code in state if code not in catalog]
    if missing:
        # The seed migration is the prerequisite, not silent self-healing here. Logged and
        # tolerated rather than raised: a company with no module rows is recoverable (the
        # resolver denies every module, which is the safe answer), while refusing to create
        # the company at all is not.
        logger.error(
            "onboarding: module catalog missing rows for %s; entity %s seeded without them",
            missing,
            entity_id,
        )

    existing = set(
        EntityFunctionMap.objects.filter(
            entity_id=entity_id, entity_function_id__in=list(catalog.values())
        ).values_list("entity_function_id", flat=True)
    )

    now = datetime.now(timezone.utc)
    rows = [
        EntityFunctionMap(
            entity_id=entity_id,
            entity_function_id=fn_id,
            # Explicit, always. The column's DATABASE default is `true`.
            is_enabled=False,
            enabled_at=None,
            disabled_at=now,
            created_by=str(user_id) if user_id else None,  # the person, never a label
            created_at=now,
            updated_at=now,
        )
        for code, fn_id in catalog.items()
        if fn_id not in existing
    ]
    if rows:
        EntityFunctionMap.objects.bulk_create(rows)
    logger.info(
        "onboarding: seeded %s module rows (all disabled) for entity %s",
        len(rows),
        entity_id,
    )


def find_resumable_entity(user_id, name: str) -> Entity | None:
    """An in-progress entity this user already created under this name, or None.

    The idempotency key is (member, name, status='onboarding') -- deliberately narrow. It
    must not match a FINALIZED company, or re-submitting Step 1 would silently rebind the
    wizard to a live company and start editing it.
    """
    # Materialised, not a subquery: ``user_entity.entity_id`` is a UUIDField (C1) while
    # ``Entity.id`` is still a CharField until C2, and SQLite stores the two spellings
    # differently (32 hex chars vs hyphenated) -- an ``id__in=<queryset>`` matched nothing.
    entity_ids = [
        str(eid)
        for eid in UserEntity.objects.filter(user_id=str(user_id)).values_list(
            "entity_id", flat=True
        )
    ]
    return Entity.objects.filter(
        id__in=entity_ids, name=name, status="onboarding"
    ).first()


def create_entity_for_user(
    user_id, entity_name, country_code: str, currency_id: str
) -> tuple[Entity, bool]:
    """Create a company owned by ``user_id``, plus its default settings.

    Returns ``(entity, created)``. ``created`` is False when an abandoned onboarding entity
    with this name was returned instead -- the caller answers 201 either way, matching Flask,
    because from the wizard's point of view the outcome is the same: here is your company id.

    ``country_code`` is the ISO alpha-2 ``country_info`` PK and ``currency_id`` a
    ``currency_info`` uuid. Callers resolve names to these first (onboarding/services/
    resolve.py) -- both columns are FKs and will not accept a display label.

    Raises OnboardingValidationError for a missing name and ConflictError for a name clash;
    the handlers in core/exceptions.py render those as 400 and 409.
    """
    name = (entity_name or "").strip()
    if not name:
        raise OnboardingValidationError("Entity name is required.")

    existing = find_resumable_entity(user_id, name)
    if existing is not None:
        logger.info(
            "onboarding: reusing in-progress entity %s for a repeated Step 1 submit",
            existing.id,
        )
        return existing, False

    if Entity.objects.filter(name=name).exists():
        # Flask's exact wording, including the missing plural -- the wizard matches on it.
        raise ConflictError("Entity name already exist")

    # One transaction for the whole creation. Flask commits the entity before adding the
    # membership, which can leave a company nobody belongs to if the second commit fails --
    # and such a row is invisible to the wizard and to the entity list, so nothing will ever
    # reclaim it. Atomic here on purpose.
    with transaction.atomic():
        entity = Entity.objects.create(
            id=str(uuid.uuid4()),
            name=name,
            country_code=country_code or None,
            currency_id=currency_id or None,
            status="onboarding",
        )
        UserEntity.objects.create(
            user_id=str(user_id),
            entity_id=entity.id,
            role=CREATOR_ROLE,
            approved=True,
        )
        _seed_default_sales_methods(entity.id)
        _seed_module_defaults(entity.id, user_id=user_id)

    logger.info("onboarding: created entity %s for user %s", entity.id, user_id)
    return entity, True
