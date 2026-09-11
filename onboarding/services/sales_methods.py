"""Petty-cash Sales Setting -- the payment and delivery methods a shop accepts.

Ported from Minty's ``blueprints/entity/services/payment_methods.py``
(``list_sales_methods_grouped`` / ``replace_sales_methods``), plus the two ``SaleInfo``
classmethods those depend on.

RECONCILE, NEVER REPLACE

The POST is a reconciliation against the submitted name lists, not a delete-and-reinsert:

  * a name already present is matched by ``(type, lowercased name)`` and UPDATED in place;
  * a name absent from the list is SOFT-disabled (``enabled=False``), not deleted;
  * a new name is inserted.

Both properties are load-bearing. Matching by name means a method keeps its ``value_name``
and its catalog link across a rename, so historical reports still resolve the column their
figures live in. Soft-disabling means a method a shop stops accepting does not take its
history with it -- ``report_sale_detail`` rows still point at the catalog row.

WHY A CUSTOM METHOD NEEDS A CATALOG ROW

The derived ``value_name`` for a user-typed method points at a physical column that does not
exist -- "Tap & Go" becomes ``tap_&_go_sales``, and there is no such column on ``report``.
The catalog link is what makes such a method storable at all, through
``report_sale_detail`` rather than a column. So a name with no catalog match gets a
per-entity ``sale_info`` row minted for it before the method row is written.

``custom_code`` mirrors the expression the SQL backfill used, so a method minted here
collides with -- and therefore dedupes against -- its backfilled counterpart instead of
creating a second catalog row for the same thing.
"""

import logging
import uuid
from datetime import datetime, timezone

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When

from core.exceptions import AccessDeniedError, OnboardingValidationError
from core.policy import Permission, has_permission_by_user_id
from shared_models.models import EntitySaleSetting, SaleInfo

logger = logging.getLogger("minty-onboarding")

#: The two types the wizard's Sales Setting step manages. 'Cash' is deliberately NOT here:
#: it is seeded at entity creation, it is the only figure in the closing-balance formula,
#: and it publishes to Xero against its own account -- so it is not something the user
#: switches off from this screen.
MANAGED_TYPES = ("Electronic", "Delivery")


def _clean(names) -> list[str]:
    """Trimmed, de-duplicated, order-preserving. Blank entries dropped.

    De-duplication is case-insensitive but the FIRST spelling wins, because that is the one
    the user typed and the one they will expect to see echoed back.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        text = str(raw or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def custom_code(name) -> str:
    """The catalog code for an entity-invented method name.

    Mirrors the SQL backfill's expression exactly. Changing it would stop new methods
    deduping against backfilled rows and quietly create a second catalog row per method.
    """
    base = "".join(ch if ch.isalnum() else "_" for ch in (name or "").strip())
    return ("CUSTOM_" + base.upper())[:49]


def resolve_catalog_row(entity_id: str, name: str) -> SaleInfo | None:
    """The catalog row this entity should use for ``name``, or None.

    Prefers the entity's OWN row over the global one, so a custom method can shadow a
    global code. Ordering by ``entity_id IS NOT NULL`` descending is what puts the
    entity-owned row first and the global fallback second.
    """
    # ORDER BY on a computed rank rather than on entity_id itself: the global row has
    # entity_id NULL, and NULL ordering differs between backends, so ranking explicitly is
    # what makes "entity-owned wins" true on Postgres and on SQLite alike.
    entity_owned_first = Case(
        When(entity_id__isnull=True, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    return (
        SaleInfo.objects.filter(Q(entity_id=entity_id) | Q(entity_id__isnull=True))
        .filter(name__iexact=(name or "").strip())
        .annotate(_rank=entity_owned_first)
        .order_by("_rank")
        .first()
    )


def ensure_custom_catalog_row(entity_id: str, name: str, method_type: str) -> SaleInfo:
    """Get-or-create the per-entity catalog row for a user-typed method.

    Does not commit -- the caller owns the transaction, so the catalog row and the method
    row that references it land together or not at all.
    """
    code = custom_code(name)
    existing = SaleInfo.objects.filter(entity_id=entity_id, code=code).first()
    if existing is not None:
        return existing
    return SaleInfo.objects.create(
        id=str(uuid.uuid4()),
        entity_id=entity_id,
        code=code,
        name=(name or "").strip() or "Custom",
        type=method_type or "Electronic",
        # Custom methods have no physical column. This is the whole reason the catalog
        # link exists -- see the module header.
        legacy_column=None,
        is_active=True,
        display_order=0,
    )


def grouped_methods(entity_id: str) -> dict:
    """Enabled Electronic/Delivery method names, grouped by type. NO permission check.

    Split out from :func:`list_grouped` so ``/state`` can reuse the query without the
    permission gate -- resume must work for an invited cashier, who lacks
    SALES_METHOD_VIEW. Callers that ARE the sales-methods endpoint go through
    ``list_grouped``, which checks first.
    """
    rows = (
        EntitySaleSetting.objects.filter(
            entity_id=entity_id, enabled=True, type__in=MANAGED_TYPES
        )
        .order_by("display_order", "create_date")
        .values_list("type", "sale_name")
    )
    return {
        "electronic": [name for typ, name in rows if typ == "Electronic"],
        "delivery": [name for typ, name in rows if typ == "Delivery"],
    }


def list_grouped(user_id, entity_id: str) -> dict:
    """:func:`grouped_methods`, gated on SALES_METHOD_VIEW. For the endpoint."""
    if not has_permission_by_user_id(user_id, Permission.SALES_METHOD_VIEW, entity_id):
        raise AccessDeniedError("Access denied")
    return grouped_methods(entity_id)


def replace(user_id, entity_id: str, electronic, delivery) -> dict:
    """Reconcile the entity's methods to the given name lists. Returns what was applied.

    Note the permission: SALES_METHOD_CREATE, not _UPDATE. Ported as-is -- both require
    accountant, so the distinction makes no difference to who gets through, and changing it
    would be a behaviour change disguised as tidying.
    """
    if not has_permission_by_user_id(user_id, Permission.SALES_METHOD_CREATE, entity_id):
        raise AccessDeniedError("Access denied")
    if not isinstance(electronic, list) or not isinstance(delivery, list):
        raise OnboardingValidationError("electronic and delivery must be arrays")

    desired = {"Electronic": _clean(electronic), "Delivery": _clean(delivery)}
    now = datetime.now(timezone.utc)

    with transaction.atomic():
        existing = list(
            EntitySaleSetting.objects.filter(entity_id=entity_id, type__in=MANAGED_TYPES)
        )
        by_key = {
            (row.type, (row.sale_name or "").strip().lower()): row for row in existing
        }

        desired_keys: set[tuple[str, str]] = set()
        for method_type, names in desired.items():
            for index, name in enumerate(names):
                key = (method_type, name.lower())
                desired_keys.add(key)
                row = by_key.get(key)

                if row is not None:
                    # Matched: update in place so value_name and the catalog link survive a
                    # change of capitalisation or spacing.
                    row.enabled = True
                    row.sale_name = name
                    row.display_order = index + 1
                    row.updated_at = now
                    row.save(
                        update_fields=[
                            "enabled", "sale_name", "display_order", "updated_at",
                        ]
                    )
                    continue

                catalog = resolve_catalog_row(entity_id, name)
                if catalog is None:
                    catalog = ensure_custom_catalog_row(entity_id, name, method_type)

                EntitySaleSetting.objects.create(
                    sale_id=str(uuid.uuid4()),
                    entity_id=entity_id,
                    sale_name=name,
                    value_name=(
                        catalog.legacy_column
                        if catalog is not None and catalog.legacy_column
                        else name.lower().replace(" ", "_") + "_sales"
                    ),
                    type=method_type,
                    sale_info_id=catalog.id if catalog is not None else None,
                    enabled=True,
                    display_order=index + 1,
                    create_date=now,
                    updated_at=now,
                )

        # Anything previously enabled and now absent is SOFT-disabled. Never deleted --
        # report_sale_detail rows still reference it.
        for key, row in by_key.items():
            if key not in desired_keys and row.enabled:
                row.enabled = False
                row.updated_at = now
                row.save(update_fields=["enabled", "updated_at"])

    logger.info(
        "sales methods: entity=%s electronic=%s delivery=%s",
        entity_id,
        len(desired["Electronic"]),
        len(desired["Delivery"]),
    )
    return desired
