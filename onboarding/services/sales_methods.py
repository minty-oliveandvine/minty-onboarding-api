"""Petty-cash Sales Setting -- the payment and delivery methods a shop accepts.

Ported from Minty's ``blueprints/entity/services/payment_methods.py``
(``list_sales_methods_grouped`` / ``replace_sales_methods``), plus the two ``SaleInfo``
classmethods those depend on.

RECONCILE, NEVER REPLACE

The POST is a reconciliation against the submitted name lists, not a delete-and-reinsert:

  * a name already present (case-insensitively, in the catalogue) keeps its link, switched on;
  * a name absent from the list is switched OFF (``is_active=False``), never deleted;
  * a new name gets a catalogue row and a link.

Switching off rather than deleting is load-bearing: a method a shop stops accepting does not
take its history with it -- ``report_sale`` rows still point at the catalogue row.

THE CATALOGUE IS GLOBAL (C3 of Minty's docs/modernisation_plan.md)

``sale_info`` holds one row per method NAME for every company (``sale_name`` is unique);
``entity_sale_setting`` is the company's link to a row - on/off and order, nothing else. A
name nobody has used before becomes a new catalogue row for everyone; a name another company
already typed is simply linked. Types are the ``sale_type`` enum: ``electronic`` /
``delivery`` / ``other`` (Cash lives in ``other``).
"""

import logging
import uuid

from django.db import transaction

from core.exceptions import AccessDeniedError, OnboardingValidationError
from core.policy import Permission, has_permission_by_user_id
from shared_models.enums import SaleType
from shared_models.models import EntitySaleSetting, SaleInfo

logger = logging.getLogger("minty-onboarding")

#: The two types the wizard's Sales Setting step manages. Cash (type 'other') is deliberately
#: NOT here: it is linked at entity creation, it is the only figure in the closing-balance
#: formula, and it publishes to Xero against its own account -- so it is not something the
#: user switches off from this screen.
MANAGED_TYPES = (SaleType.ELECTRONIC, SaleType.DELIVERY)


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


def resolve_catalog_row(name: str) -> SaleInfo | None:
    """The catalogue row for a display name, case-insensitively; None if nobody has it."""
    text = (name or "").strip()
    if not text:
        return None
    return SaleInfo.objects.filter(sale_name__iexact=text).first()


def ensure_catalog_row(name: str, method_type, *, value_name=None, display_order=None) -> SaleInfo:
    """Get-or-create the GLOBAL catalogue row for ``name``.

    Does not commit -- the caller owns the transaction, so the catalogue row and the link
    that references it land together or not at all.
    """
    existing = resolve_catalog_row(name)
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

    with transaction.atomic():
        by_sale_id = {link.sale_id: link for link in _links(entity_id, enabled_only=False)}
        wanted: set = set()
        for method_type, names in desired.items():
            for index, name in enumerate(names):
                catalog = ensure_catalog_row(name, method_type)
                wanted.add(catalog.id)
                link = by_sale_id.get(catalog.id)
                if link is None:
                    link = EntitySaleSetting(entity_id=entity_id, sale=catalog)
                    by_sale_id[catalog.id] = link
                link.is_active = True
                link.display_order = index + 1
                link.save()

        # Anything previously enabled and now absent is switched off. Never deleted --
        # report_sale rows still reference the catalogue row.
        for sale_id, link in by_sale_id.items():
            if sale_id not in wanted and link.is_active:
                link.is_active = False
                link.save(update_fields=["is_active"])

    logger.info(
        "sales methods: entity=%s electronic=%s delivery=%s",
        entity_id, len(desired[SaleType.ELECTRONIC]), len(desired[SaleType.DELIVERY]),
    )
    return {"electronic": desired[SaleType.ELECTRONIC], "delivery": desired[SaleType.DELIVERY]}
