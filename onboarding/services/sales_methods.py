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

THE CATALOGUE IS GLOBAL (C3 of Minty's docs/modernisation/modernisation_plan.md)

``sale_info`` holds one row per method NAME for every company (``sale_name`` is unique);
``entity_sale_setting`` is the company's link to a row - on/off and order, nothing else. A
name nobody has used before becomes a new catalogue row for everyone; a name another company
already typed is simply linked. Types are the ``sale_type`` enum: ``electronic`` /
``delivery`` / ``other`` (Cash lives in ``other``).
"""

import logging
import re
import uuid

from django.db import transaction
from minty_db.enums import SaleType
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityPettycashSettings,
    EntitySaleSetting,
    SaleInfo,
)

from core.exceptions import AccessDeniedError, ConflictError, OnboardingValidationError
from core.policy import Permission, has_permission_by_user_id

logger = logging.getLogger("minty-onboarding")

#: The two types the wizard's Sales Setting step manages. Cash (type 'other') is deliberately
#: NOT here: it is linked at entity creation, it is the only figure in the closing-balance
#: formula, and it publishes to Xero against its own account -- so it is not something the
#: user switches off from this screen.
MANAGED_TYPES = (SaleType.ELECTRONIC, SaleType.DELIVERY)
CASH_VALUE_NAME = "cash_sales"
CASH_IS_STEP_4 = "Cash is set by your answer in Accounting Setup."
FROM_STEP_4 = "Your sales categories come from Accounting Setup. Change them there."
#: Sales categories that are sales methods: other income (interest and the like) isn't taken at the till.
CATEGORY_TYPES = ("sales", "revenue")
SALE_NAME_MAX = 80


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
        "categories": categories(entity_id),
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
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is not None and entity.setup_mode:
        # Step 5 shows step 4's sales categories (owner, 2026-11-11): they change there.
        raise ConflictError(FROM_STEP_4)

    desired = {SaleType.ELECTRONIC: _clean(electronic), SaleType.DELIVERY: _clean(delivery)}
    for names in desired.values():
        for name in names:
            row = resolve_catalog_row(name)
            if name.lower() == "cash" or (row is not None and row.value_name == CASH_VALUE_NAME):
                # Cash follows step 4's cash answer (accounting._cash_method), never this list:
                # typed here it would switch Cash on for a company banking all its sales.
                raise OnboardingValidationError(CASH_IS_STEP_4)

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
        entity_id,
        len(desired[SaleType.ELECTRONIC]),
        len(desired[SaleType.DELIVERY]),
    )
    return {"electronic": desired[SaleType.ELECTRONIC], "delivery": desired[SaleType.DELIVERY]}


# ---- step 5 = step 4's sales categories (owner, 2026-11-11) -------------------------------------
#
# A company that chose how Minty keeps its books (step 4) has one sales method per sales category:
# each linked to its account by ``entity_sale_setting.account_id`` (minty-db v0.6). Every step-4
# change re-links them here (``api_accounting`` calls it after each change, in its transaction).


def _method_type(name: str) -> str:
    """A new catalogue row's type, by 4.3's glyph rule. Petty Cash's daily closing still groups (and
    shows) only electronic and delivery methods, so a category is always one of the two."""
    return SaleType.DELIVERY if re.search(r"deliver", name, re.IGNORECASE) else SaleType.ELECTRONIC


def _category_accounts(entity: Entity) -> list[AccountInfo]:
    """Step 4's sales categories in 4.3's order: a Minty company's own; on Xero, X2's ticks."""
    from onboarding.services import accounting

    rows = list(
        accounting._active(entity.id)
        .filter(account_type__in=CATEGORY_TYPES, is_system=False)
        .order_by("code", "name")
    )
    if accounting.minty_on_its_own(entity):
        return rows
    used = accounting._used_ids(entity.id)
    return [a for a in rows if a.xero_account_id and a.id in used]


def sync_from_accounts(entity: Entity) -> None:
    """Re-link the company's sales methods to step 4's sales categories. Links are switched off and
    unlinked, never deleted (``report_sale`` keeps pointing at them). A company with no mode keeps
    step 5's own list."""
    if not entity.setup_mode:
        return
    settings = EntityPettycashSettings.objects.filter(entity_id=entity.id).first()
    cash_account = settings.cash_sale_account_id if settings else None
    wanted: dict = {}  # catalogue row id -> (account, place)
    for place, account in enumerate((a for a in _category_accounts(entity) if a.id != cash_account), start=1):
        name = (account.name or "").strip()[:SALE_NAME_MAX]
        catalog = ensure_catalog_row(name, _method_type(name))
        if catalog.value_name == CASH_VALUE_NAME:
            continue  # an account named like Cash is not a second Cash
        wanted.setdefault(catalog.id, (account, place))
    links = list(EntitySaleSetting.objects.select_for_update().filter(entity_id=entity.id).select_related("sale"))
    cash = next((link for link in links if link.sale.value_name == CASH_VALUE_NAME), None)
    # First unlink (one method per account per company), then link.
    for link in links:
        if link is cash:
            continue
        target = wanted.get(link.sale_id)
        if target is None or link.account_id != target[0].id:
            if link.account_id is not None or (target is None and link.is_active):
                EntitySaleSetting.objects.filter(entity_id=entity.id, sale_id=link.sale_id).update(
                    account_id=None, is_active=target is not None and link.is_active
                )
    if cash is not None and cash.account_id != cash_account:
        EntitySaleSetting.objects.filter(entity_id=entity.id, sale_id=cash.sale_id).update(account_id=cash_account)
    for sale_id, (account, place) in wanted.items():
        updated = EntitySaleSetting.objects.filter(entity_id=entity.id, sale_id=sale_id).update(
            account_id=account.id, is_active=True, display_order=place
        )
        if not updated:
            EntitySaleSetting.objects.create(
                entity_id=entity.id, sale_id=sale_id, account_id=account.id, is_active=True, display_order=place
            )
    logger.info("sales methods: entity=%s linked to %s sales categories", entity.id, len(wanted))


def categories(entity_id) -> list[dict]:
    """Step 5's list: the company's active methods that are a sales account, in 4.3's order (Cash
    among them when it is on and the cash-sale account is set)."""
    links = {
        link.account_id: link
        for link in EntitySaleSetting.objects.filter(
            entity_id=entity_id, is_active=True, account_id__isnull=False
        ).select_related("sale")
    }
    accounts = AccountInfo.objects.filter(id__in=list(links)).order_by("code", "name")
    return [
        {"account_id": str(a.id), "code": a.code or a.xero_code or "", "name": a.name,
         "method": links[a.id].sale.sale_name}
        for a in accounts
    ]
