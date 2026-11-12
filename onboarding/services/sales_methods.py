"""Petty-cash Sales Setting -- the payment and delivery methods a shop accepts.

Ported from Minty's ``blueprints/entity/services/payment_methods.py``
(``list_sales_methods_grouped`` / ``replace_sales_methods``), plus the two ``SaleInfo``
classmethods those depend on.

RECONCILE, NEVER REPLACE

The POST is a reconciliation against the submitted name lists, not a delete-and-reinsert:

  * a name already present (case-insensitively, Minty's or the company's own) keeps its link,
    switched on;
  * a name absent from the list: Minty's is switched OFF (``is_active=False``); the company's
    own is deleted, or switched off with its row when a report used it;
  * a new name gets a row of the company's own and a link.

Never deleting what a report used is load-bearing: a method a shop stops accepting does not
take its history with it -- ``report_sale`` rows still point at the row.

WHO OWNS A METHOD (owner, 2026-11-12; minty-db v0.7 ``sale_info.entity_id``)

``sale_info`` rows are Minty's catalogue (``entity_id`` NULL: everyone's, never renamed or
deleted) or a company's own (``entity_id`` = it: the names it typed). ``entity_sale_setting``
is the company's link to a row - on/off and order, nothing else. A typed name that is Minty's
links Minty's row; any other becomes the company's own, never shared with another company.
Types are the ``sale_type`` enum: ``electronic`` / ``delivery`` / ``other`` (Cash lives in
``other``).
"""

import logging
import uuid

from django.db import transaction
from django.db.models import F, Q
from minty_db.enums import SaleType
from minty_db.models import (
    Entity,
    EntitySaleSetting,
    ReportSale,
    SaleInfo,
)

from core.exceptions import AccessDeniedError, OnboardingValidationError
from core.policy import Permission, has_permission_by_user_id

logger = logging.getLogger("minty-onboarding")

#: The two types the wizard's Sales Setting step manages. Cash (type 'other') is deliberately
#: NOT here: it is linked at entity creation, it is the only figure in the closing-balance
#: formula, and it publishes to Xero against its own account -- so it is not something the
#: user switches off from this screen.
MANAGED_TYPES = (SaleType.ELECTRONIC, SaleType.DELIVERY)
CASH_VALUE_NAME = "cash_sales"
CASH_IS_STEP_4 = "Cash is set by your answer in Accounting Setup."
#: Which lists step 5 shows (owner, 2026-11-11): Restaurant & Food takes Electronic and Delivery; a
#: retail shop, a service or a general business takes one Non-cash list, stored as electronic (Petty
#: Cash shows electronic, delivery and Cash). A company from before the question (no type) keeps both.
NON_CASH_BUSINESSES = ("retail", "service", "general")


def groups(entity_id) -> str:
    """``non_cash`` or ``electronic_delivery`` - the lists step 5 and Petty Cash show."""
    business = Entity.objects.filter(id=entity_id).values_list("business_type", flat=True).first()
    return "non_cash" if business in NON_CASH_BUSINESSES else "electronic_delivery"


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


def value_name_for(name: str) -> str:
    """The form-field name a NEW method gets: "Tap & Go" -> ``tap_&_go_sales`` (the derivation
    Minty has always used, so both services mint the same key)."""
    return (name or "").strip().lower().replace(" ", "_") + "_sales"


def resolve_catalog_row(name: str, entity_id=None) -> SaleInfo | None:
    """The row a company means by a display name, case-insensitively: Minty's catalogue first
    (``entity_id`` NULL, minty-db v0.7), then the company's own enabled row. Another company's own
    row is never found. None if neither has it."""
    text = (name or "").strip()
    if not text:
        return None
    mine = Q(entity_id=entity_id, enabled=True) if entity_id else Q(pk__in=[])
    return (
        SaleInfo.objects.filter(Q(entity_id__isnull=True) | mine, sale_name__iexact=text)
        .order_by(F("entity_id").asc(nulls_first=True))
        .first()
    )


def ensure_catalog_row(name: str, method_type, *, value_name=None, display_order=None, entity_id=None) -> SaleInfo:
    """Get-or-create the row for ``name``: Minty's or the company's own; else a new row - the
    company's own when ``entity_id`` is given (a typed name, owner 2026-11-12), Minty's when not
    (the default Cash only).

    Does not commit -- the caller owns the transaction, so the row and the link that references
    it land together or not at all.
    """
    existing = resolve_catalog_row(name, entity_id)
    if existing is not None:
        return existing
    clean = (name or "").strip() or "Custom"
    return SaleInfo.objects.create(
        id=uuid.uuid4(),
        sale_name=clean,
        type=str(method_type or SaleType.OTHER),
        value_name=value_name or value_name_for(clean),
        display_order=display_order,
        enabled=True,
        entity_id=entity_id,
    )


def _links(entity_id: str, *, enabled_only: bool):
    qs = EntitySaleSetting.objects.filter(entity_id=entity_id, sale__type__in=MANAGED_TYPES)
    if enabled_only:
        qs = qs.filter(is_active=True)
    return qs.select_related("sale").order_by("display_order", "sale__sale_name")


def grouped_methods(entity_id: str) -> dict:
    """Enabled electronic / delivery method names, grouped by type. NO permission check.

    Split out from :func:`list_grouped` so ``/state`` can reuse the query without the
    permission gate -- resume must work for an invited cashier, who lacks
    SALES_METHOD_VIEW. Callers that ARE the sales-methods endpoint go through
    ``list_grouped``, which checks first.
    """
    rows = [(link.sale.type, link.sale.sale_name) for link in _links(entity_id, enabled_only=True)]
    return {
        "electronic": [name for typ, name in rows if typ == SaleType.ELECTRONIC],
        "delivery": [name for typ, name in rows if typ == SaleType.DELIVERY],
        "groups": groups(entity_id),
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

    desired = {SaleType.ELECTRONIC: _clean(electronic), SaleType.DELIVERY: _clean(delivery)}
    for names in desired.values():
        for name in names:
            row = resolve_catalog_row(name, entity_id)
            if name.lower() == "cash" or (row is not None and row.value_name == CASH_VALUE_NAME):
                # Cash follows step 4's cash answer (accounting._cash_method), never this list:
                # typed here it would switch Cash on for a company banking all its sales.
                raise OnboardingValidationError(CASH_IS_STEP_4)

    with transaction.atomic():
        by_sale_id = {link.sale_id: link for link in _links(entity_id, enabled_only=False)}
        wanted: set = set()
        for method_type, names in desired.items():
            for index, name in enumerate(names):
                catalog = ensure_catalog_row(name, method_type, entity_id=entity_id)
                wanted.add(catalog.id)
                link = by_sale_id.get(catalog.id)
                if link is None:
                    link = EntitySaleSetting(entity_id=entity_id, sale=catalog)
                    by_sale_id[catalog.id] = link
                link.is_active = True
                link.display_order = index + 1
                link.save()

        # Anything previously enabled and now absent: Minty's is switched off (the row is
        # everyone's); the company's own is deleted, or switched off with its row when a report
        # used it (report_sale still points at it) - owner, 2026-11-12.
        for sale_id, link in by_sale_id.items():
            if sale_id in wanted or not link.is_active:
                continue
            if link.sale.entity_id is None or ReportSale.objects.filter(sale_id=sale_id).exists():
                link.is_active = False
                link.save(update_fields=["is_active"])
                if link.sale.entity_id is not None:
                    SaleInfo.objects.filter(id=sale_id).update(enabled=False)
            else:
                EntitySaleSetting.objects.filter(entity_id=entity_id, sale_id=sale_id).delete()
                SaleInfo.objects.filter(id=sale_id).delete()

    logger.info(
        "sales methods: entity=%s electronic=%s delivery=%s",
        entity_id,
        len(desired[SaleType.ELECTRONIC]),
        len(desired[SaleType.DELIVERY]),
    )
    return {"electronic": desired[SaleType.ELECTRONIC], "delivery": desired[SaleType.DELIVERY]}
