"""Accounting Setup - wizard step 4 when Xero is optional (Figma 348:1007, 2026-11-10).

The design is Minty's ``docs/modernisation/xero_optional_database.md``: Minty owns its accounts,
and Xero becomes optional. Step 4 asks how Minty should work (4.1). On its own, the company
answers its business type and the cash question (4.2), picks its sales and expense accounts
(4.3, 4.4: Auto Fill from ``account_template``, or its own) and the five accounts daily closing
needs (4.5). With Xero, step 4 is today's connect and lists (``xero_lists``).

**The new columns and the old readers (EXPAND).** Until each module's own release moves it, the
apps read the Xero-shaped columns, so an account made here is written both ways:
- ``account_info``: Minty's ``code``/``account_type``/``record_status``/``source`` AND the old
  ``xero_code``/``type`` (Xero's spelling)/``status``. Its ``xero_account_id`` stays empty: it is
  not in Xero.
- an expense account is also ticked for Petty Cash (``entity_account_xero``) and offered to
  Payment Request as a bill code (``entity_bill_account_xero`` with no Xero id, as a hand-added
  code is), and gets its ``account_module`` rows.

The owner's answers (2026-11-10): the cash question is on 4.2 for every company ("Not sure" is
stored as with Cash Sales); expense accounts show under two headings derived from their type
(``direct_costs`` = "Cost of what you sell", every other expense type = "Running the business");
"None of these" starts with only the shared system rows, and Auto Fill on an empty list offers
the General set. Each answer is ``(body, status)``, as ``xero_lists`` returns them; sentences are
Part 11's (issue, then action).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q
from minty_db.models import (
    AccountInfo,
    AccountModule,
    AccountTemplate,
    BillLine,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    EntitySaleSetting,
    ReportExpense,
)

# the account rules every backend checks (minty_shared.accounts, v0.5; owner 2026-11-12: one copy,
# shared with the hub's Settings)
from minty_shared import accounts as accounts_rules
from minty_shared.accounts import (
    TYPE_WORDS,
    XERO_TYPE,
)

from core import xero_client
from core.policy import Permission, has_permission_by_user_id
from onboarding.services.xero_lists import xero_connected

logger = logging.getLogger("minty-onboarding")

DENIED = {"error": "Access denied"}
NOT_FOUND = {"error": "Entity not found"}

MODES = ("minty", "xero")
BUSINESS_TYPES = ("fnb", "retail", "service", "general")

#: The account lists of 4.3 and 4.4, by Minty account type.
KINDS = {
    "sales": ("sales", "revenue", "other_income"),
    "expense": ("direct_costs", "expense", "overheads"),
}
#: 4.4's two headings, from the type (no stored group: decision 19).
GROUP_OF = {
    "direct_costs": "cost_of_sales",
    "expense": "running_costs",
    "overheads": "running_costs",
}

#: The five accounts daily closing needs (4.5): the settings column, and the types each takes.
SLOTS = {
    "petty_cash": ("pettycash_account_id", ("bank",)),
    "bank": ("bank_account_id", ("bank",)),
    "cash_sale": ("cash_sale_account_id", KINDS["sales"]),
    "discrepancy": ("discrepancy_account_id", KINDS["expense"]),
    "director": (
        "director_account_id",
        ("current_liability", "liability", "term_liability", "current", "noncurrent"),
    ),
}

MINTY_MADE = ("template", "user")


# ---- reading -----------------------------------------------------------------------------------


def _entity(entity_id) -> Entity | None:
    return Entity.objects.filter(id=entity_id).first()


def _active(entity_id):
    return AccountInfo.objects.filter(entity_id=entity_id, record_status="active")


def _kind_of(account_type: str | None) -> str | None:
    return next((k for k, types in KINDS.items() if account_type in types), None)


def _used_ids(entity_id) -> set:
    """The Xero accounts Minty uses (X2, X3): Petty Cash's ``account_module`` rows, and the old
    wizard's expense ticks (``entity_account_xero``) for a company that ticked before X3."""
    modules = AccountModule.objects.filter(
        entity_id=entity_id, module_code="PETTY_CASH", is_active=True
    ).values_list("account_id", flat=True)
    old = EntityAccountXero.objects.filter(
        account__entity_id=entity_id, is_active=True
    ).values_list("account_id", flat=True)
    return set(modules) | set(old)


def _held(entity_id, ids) -> set:
    """Of ``ids``, the accounts something uses (``_used``'s rule, three queries for the lot)."""
    ids = list(ids)
    if not ids:
        return set()
    held = set(
        ReportExpense.objects.filter(account_id__in=ids).values_list("account_id", flat=True)
    )
    held |= set(BillLine.objects.filter(account_id__in=ids).values_list("account_id", flat=True))
    settings = (
        EntityPettycashSettings.objects.filter(entity_id=entity_id)
        .values(*SETTINGS_COLUMNS)
        .first()
    )
    held |= {v for v in (settings or {}).values() if v in set(ids)}
    return held


def _row(account: AccountInfo, used: bool = True) -> dict:
    return {
        # A row the company made in Minty is in use by being there; a Xero row once it is ticked.
        "used": used,
        "id": str(account.id),
        "code": account.code or "",
        "name": account.name,
        "type": account.account_type,
        "type_label": TYPE_WORDS.get(account.account_type or "", ""),
        "description": account.description or "",
        "kind": _kind_of(account.account_type),
        "group": GROUP_OF.get(account.account_type or ""),
        "source": account.source,
        "is_system": bool(account.is_system),
        "from_xero": bool(account.xero_account_id),
    }


def _code_number(code: str | None) -> int | None:
    return int(code) if code and code.isdigit() else None


def next_code(entity_id, account_type: str) -> str:
    """The next free code for a type (4.3b): ``minty_shared.accounts.next_code`` over the company's
    active codes."""
    return accounts_rules.next_code(_active(entity_id).values_list("code", "account_type"), account_type)


def _settings(entity_id) -> EntityPettycashSettings | None:
    return EntityPettycashSettings.objects.filter(entity_id=entity_id).first()


def answers(entity: Entity) -> dict:
    return {
        "mode": entity.setup_mode,
        "business_type": entity.business_type,
        "uses_sales_cash": entity.uses_sales_cash,
        "business_answered": entity.uses_sales_cash is not None,
    }


def setup(user_id, entity_id) -> tuple[dict, int]:
    """Everything step 4 draws, in one read: the answers, both lists, the five accounts and the
    options for each, and the next free code per type."""
    if not has_permission_by_user_id(user_id, Permission.COA_VIEW, entity_id):
        return DENIED, 403
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    accounts = list(_active(entity_id).order_by("code", "name"))
    used = _used_ids(entity_id)
    held = _held(entity_id, (a.id for a in accounts if a.source == "template"))
    lists = {
        kind: [
            {
                **_row(a, used=not a.xero_account_id or a.id in used),
                # what Revert would take out: an Auto Fill row nothing uses yet
                "auto_filled": a.source == "template" and a.id not in held,
            }
            for a in accounts
            if a.account_type in types and not a.is_system
        ]
        for kind, types in KINDS.items()
    }
    settings = _settings(entity_id)
    by_id = {a.id: a for a in accounts}
    slots = {}
    for role in _slot_roles(entity):
        column, types = SLOTS[role]
        chosen = by_id.get(getattr(settings, column, None)) if settings else None
        slots[role] = {
            "account": _row(chosen) if chosen else None,
            "options": [_row(a) for a in accounts if a.account_type in types],
        }
    types = sorted(
        {t for ts in KINDS.values() for t in ts} | {t for _c, ts in SLOTS.values() for t in ts}
    )
    return {
        **answers(entity),
        "connected": xero_connected(entity),
        "accounts": lists,
        "slots": slots,
        "slots_ready": sum(1 for s in slots.values() if s["account"]),
        "next_code": {t: next_code(entity_id, t) for t in types},
        "types": [{"value": t, "label": TYPE_WORDS[t]} for t in types],
    }, 200


# ---- 4.1 and 4.2: the answers -----------------------------------------------------------------


def set_mode(user_id, entity_id, mode) -> tuple[dict, int]:
    """4.1: how Minty keeps the books - ``minty`` (on its own) or ``xero``."""
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    if mode not in MODES:
        return {
            "error": "Choose how Minty should work.",
            "fields": {"mode": "Choose how Minty should work."},
        }, 400
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    if mode == "minty" and entity.xero_org_id:
        return {
            "error": "This company is connected to Xero. Disconnect Xero first to use Minty on its own."
        }, 409
    with transaction.atomic():
        if mode == "xero" and entity.setup_mode != "xero":
            refused = _clear_own_chart(entity_id)
            if refused:
                return {"error": refused}, 409
        if entity.setup_mode != mode:
            entity.setup_mode = mode
            entity.save(update_fields=["setup_mode"])
            logger.info("accounting: entity %s set up with %s", entity_id, mode)
    return answers(entity), 200


#: Switching to Xero once Minty's accounts carry reports or bills (they are never deleted).
IN_USE_BY_RECORDS = (
    "This company's reports or bills already use its accounts in Minty, so it keeps using Minty "
    "on its own for now. Connect Xero from Settings once setup is finished."
)


def _clear_own_chart(entity_id) -> str | None:
    """Onboarding's choice is free (owner, 2026-11-11): choosing Xero takes away the accounts Minty
    made for the company - the business type's set, Auto Fill's and the person's own - and the
    daily-closing choices that pointed at them, with no question asked. The hidden system rows
    stay. A company whose reports or bills already use one of them keeps everything and is told
    why (the sentence; None when cleared)."""
    own = list(
        AccountInfo.objects.filter(entity_id=entity_id, source__in=MINTY_MADE, is_system=False)
    )
    if not own:
        return None
    ids = [a.id for a in own]
    if (
        ReportExpense.objects.filter(account_id__in=ids).exists()
        or BillLine.objects.filter(account_id__in=ids).exists()
    ):
        return IN_USE_BY_RECORDS
    settings = _settings(entity_id)
    if settings is not None:
        cleared = [c for c in SETTINGS_COLUMNS if getattr(settings, c) in set(ids)]
        for column in cleared:
            setattr(settings, column, None)
        if cleared:
            settings.save(update_fields=cleared)
    for account in own:
        _remove(account)
    logger.info("accounting: entity %s chose Xero; %s Minty accounts removed", entity_id, len(own))
    return None


def _cash_method(entity_id, on: bool) -> None:
    """The company's Cash sales method follows the cash answer (owner's brief, 2026-11-10): "We bank
    all sales" turns it off, so Petty Cash asks for no cash sales; yes turns it on again, linking it
    when the company has no link yet (as a new company gets in entity_create)."""
    from onboarding.services.entity_create import DEFAULT_SALES_METHODS
    from onboarding.services.sales_methods import ensure_catalog_row

    name, value_name, sale_type, order = next(m for m in DEFAULT_SALES_METHODS if m[1] == "cash_sales")
    changed = EntitySaleSetting.objects.filter(
        entity_id=entity_id, sale__value_name=value_name
    ).update(is_active=on)
    if on and not changed:
        catalog = ensure_catalog_row(name, sale_type, value_name=value_name, display_order=order)
        EntitySaleSetting.objects.create(
            entity_id=entity_id, sale=catalog, is_active=True, display_order=order
        )


def _slot_roles(entity: Entity) -> list[str]:
    """Daily closing's accounts for this company: a company banking all its sales has no cash
    sales, so no cash-sale account."""
    return [r for r in SLOTS if not (r == "cash_sale" and entity.uses_sales_cash is False)]


def set_business(user_id, entity_id, business_type, uses_sales_cash) -> tuple[dict, int]:
    """4.2: the business type (empty = "None of these") and the cash question ("Not sure" arrives as
    true). On the Minty path the shared system accounts are added then, hidden: the company starts
    with them, and its lists stay empty until Auto Fill or its own accounts."""
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    fields = {}
    if business_type not in (*BUSINESS_TYPES, None, ""):
        fields["business_type"] = "Choose what best describes your business."
    if not isinstance(uses_sales_cash, bool):
        fields["uses_sales_cash"] = "Choose how you handle cash sales to continue."
    if fields:
        return {"error": next(iter(fields.values())), "fields": fields}, 400
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    with transaction.atomic():
        entity.business_type = business_type or None
        entity.uses_sales_cash = uses_sales_cash
        entity.save(update_fields=["business_type", "uses_sales_cash"])
        _cash_method(entity.id, uses_sales_cash)
        added = _add_shared_rows(user_id, entity) if entity.setup_mode == "minty" else 0
        if entity.setup_mode == "minty":
            added += _sales_from_set(user_id, entity)
    logger.info(
        "accounting: entity %s answered business=%s cash=%s (%s system accounts added)",
        entity_id,
        entity.business_type,
        uses_sales_cash,
        added,
    )
    return answers(entity), 200


# ---- Auto Fill -----------------------------------------------------------------------------------


def _templates(entity: Entity):
    """The template rows that reach this company: its country (or every country), its set, and the
    rows shared by every set. "None of these" (no business type) reaches the General set."""
    business = entity.business_type or "general"
    cash = True if entity.uses_sales_cash is None else entity.uses_sales_cash
    return (
        AccountTemplate.objects.filter(
            Q(country_code__isnull=True) | Q(country_code=entity.country_code)
        )
        .filter(Q(business_type__isnull=True) | Q(business_type=business))
        .filter(Q(uses_sales_cash__isnull=True) | Q(uses_sales_cash=cash))
        .order_by("sort_order", "code", "name")
    )


def _shared(entity: Entity):
    return AccountTemplate.objects.filter(
        business_type__isnull=True, uses_sales_cash__isnull=True
    ).filter(Q(country_code__isnull=True) | Q(country_code=entity.country_code))


def _present(entity_id, template: AccountTemplate) -> bool:
    accounts = _active(entity_id)
    if (
        template.code
        and accounts.filter(code=template.code, name__iexact=template.name.strip()).exists()
    ):
        return True
    return not template.code and accounts.filter(name__iexact=template.name.strip()).exists()


def _clash(entity_id, code: str | None, name: str, exclude=None) -> dict:
    """The field sentences for a code or name another active account already has."""
    accounts = _active(entity_id).exclude(id=exclude) if exclude else _active(entity_id)
    by_code = accounts.filter(code=code).first() if code else None
    by_name = accounts.filter(name__iexact=name.strip()).first()
    return accounts_rules.clash_fields(code, by_code.name if by_code else None, by_name.name if by_name else None)


def autofill_options(user_id, entity_id, kind) -> tuple[dict, int]:
    """What Auto Fill offers for one list (4.3 or 4.4), ticked where the template says."""
    if not has_permission_by_user_id(user_id, Permission.COA_VIEW, entity_id):
        return DENIED, 403
    if kind not in KINDS:
        return {"error": "Choose which accounts to fill."}, 400
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    rows = [t for t in _templates(entity) if t.type in KINDS[kind] and not t.is_system]
    return {
        "kind": kind,
        "based_on": entity.business_type or "general",
        "options": [
            {
                "template_id": str(t.id),
                "code": t.code or "",
                "name": t.name,
                "type": t.type,
                "group": GROUP_OF.get(t.type),
                "description": t.description or "",
                "ticked": t.ticked_by_default,
                "added": _present(entity_id, t),
            }
            for t in rows
        ],
    }, 200


def apply_autofill(user_id, entity_id, kind, template_ids) -> tuple[dict, int]:
    """Add the chosen template rows to the company (``source='template'``). A row already there is
    left alone; one whose code or name another account holds is refused, naming it."""
    if not has_permission_by_user_id(user_id, Permission.COA_CREATE, entity_id):
        return DENIED, 403
    if kind not in KINDS:
        return {"error": "Choose which accounts to fill."}, 400
    if not isinstance(template_ids, list):
        return {"error": "Choose the accounts to add."}, 400
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    offered = {
        str(t.id): t for t in _templates(entity) if t.type in KINDS[kind] and not t.is_system
    }
    chosen = [offered[i] for i in dict.fromkeys(str(x) for x in template_ids) if i in offered]
    if len(chosen) != len(set(map(str, template_ids))):
        return {
            "error": "Some of those accounts are not offered for this company. Reload the list and try again."
        }, 400
    added = []
    with transaction.atomic():
        for template in chosen:
            if _present(entity_id, template):
                continue
            fields = _clash(entity_id, template.code, template.name)
            if fields:
                transaction.set_rollback(True)
                return {"error": next(iter(fields.values())), "fields": fields}, 409
            added.append(_from_template(user_id, entity, template))
    logger.info("accounting: entity %s auto-filled %s %s account(s)", entity_id, len(added), kind)
    return {"added": [_row(a) for a in added]}, 200


#: ``entity_pettycash_settings``' account columns: daily closing's five and the discrepancy bank.
SETTINGS_COLUMNS = [c for c, _t in SLOTS.values()] + ["discrepancy_bank_account_id"]


def _used(account_id) -> bool:
    return (
        ReportExpense.objects.filter(account_id=account_id).exists()
        or BillLine.objects.filter(account_id=account_id).exists()
        or EntityPettycashSettings.objects.filter(
            Q(**{SETTINGS_COLUMNS[0]: account_id})
            | Q(bank_account_id=account_id)
            | Q(cash_sale_account_id=account_id)
            | Q(discrepancy_account_id=account_id)
            | Q(director_account_id=account_id)
            | Q(discrepancy_bank_account_id=account_id)
        ).exists()
    )


def revert(user_id, entity_id, kind) -> tuple[dict, int]:
    """Revert: take the Auto Fill rows of one list back out. Rows something already uses stay."""
    if not has_permission_by_user_id(user_id, Permission.COA_DELETE, entity_id):
        return DENIED, 403
    if kind not in KINDS:
        return {"error": "Choose which accounts to revert."}, 400
    if _entity(entity_id) is None:
        return NOT_FOUND, 404
    removed, kept = 0, []
    with transaction.atomic():
        for account in _active(entity_id).filter(
            source="template", is_system=False, account_type__in=KINDS[kind]
        ):
            if _used(account.id):
                kept.append(_row(account))
            else:
                _remove(account)
                removed += 1
    return {"removed": removed, "kept": kept}, 200


# ---- one account ---------------------------------------------------------------------------------


def _check(entity_id, payload: dict, exclude=None) -> tuple[dict, dict]:
    """The cleaned fields and the field sentences for one account's code, name, type and description:
    each field's own rule (``minty_shared.accounts.check_fields``), then a code or name taken."""
    clean, fields = accounts_rules.check_fields(payload)
    if not fields:
        fields = _clash(entity_id, clean["code"], clean["name"], exclude)
    return clean, fields


def _old_rows(
    account: AccountInfo, entity: Entity, *, previous_code=None, modules=(True, True)
) -> None:
    """Keep the apps' old readers in step with an expense account (Petty Cash's ticks, Payment
    Request's bill codes) and give it its module rows. ``modules`` = (Petty Cash, Payment Request):
    both for an account the person adds; a template row's own columns for Auto Fill (the
    accounting team's sheet, 2026-11-10). A module left out gets its rows inactive, so Settings can
    turn it on later."""
    if account.account_type not in KINDS["expense"]:
        EntityAccountXero.objects.filter(account_id=account.id).delete()
        return
    petty, bills = modules
    EntityAccountXero.objects.update_or_create(
        account_id=account.id,
        defaults={"name": account.name, "type": account.type, "is_active": petty},
    )
    bill = EntityBillAccountXero.objects.filter(
        entity_id=entity.id,
        account_code=previous_code or account.code,
        xero_account_id="",
        is_deleted=False,
    ).first()
    if bill is None:
        EntityBillAccountXero.objects.create(
            entity_id=entity.id,
            account_code=account.code,
            account_name=account.name,
            account_type=account.type,
            xero_account_id="",
            is_active=bills,
        )
    else:
        bill.account_code, bill.account_name, bill.account_type = (
            account.code,
            account.name,
            account.type,
        )
        bill.save(update_fields=["account_code", "account_name", "account_type"])
    for module, on in (("PETTY_CASH", petty), ("PAYMENT_REQUEST", bills)):
        AccountModule.objects.get_or_create(
            account_id=account.id,
            module_code=module,
            defaults={"entity_id": entity.id, "is_active": on},
        )


def _create(
    user_id,
    entity,
    code,
    name,
    account_type,
    description,
    *,
    source,
    is_system=False,
    modules=(True, True),
) -> AccountInfo:
    account = AccountInfo.objects.create(
        entity_id=entity.id,
        name=name,
        description=description,
        code=code,
        account_type=account_type,
        type=XERO_TYPE[account_type],
        xero_code=code,
        status="ACTIVE",
        source=source,
        is_system=is_system,
        created_by=user_id,
    )
    if not is_system:
        _old_rows(account, entity, modules=modules)
    return account


def _remove(account: AccountInfo) -> None:
    """Delete an account nothing uses, with its old-reader rows."""
    EntityBillAccountXero.objects.filter(
        entity_id=account.entity_id, account_code=account.code, xero_account_id=""
    ).delete()
    EntityAccountXero.objects.filter(account_id=account.id).delete()
    AccountModule.objects.filter(account_id=account.id).delete()
    account.delete()


#: The float and the bank it is deposited into are two accounts (Petty Cash Settings' rule too).
SAME_BANK = (
    "Petty cash and the bank you deposit into can't be the same account. "
    "Choose a different one for each."
)


def _assign(entity_id, role, account: AccountInfo | None) -> dict:
    column, types = SLOTS[role]
    if account is not None and account.account_type not in types:
        return {"account_id": "That account can't be used for this. Choose one from the list."}
    settings, _ = EntityPettycashSettings.objects.get_or_create(entity_id=entity_id)
    other = {"petty_cash": "bank", "bank": "petty_cash"}.get(role)
    if account is not None and other and getattr(settings, SLOTS[other][0]) == account.id:
        return {"account_id": SAME_BANK}
    setattr(settings, column, account.id if account else None)
    fields = [column.removesuffix("_id")]
    if role == "petty_cash":
        # A cash difference settles against the float itself: the old step 6 set both, and Petty
        # Cash and finalize still read the discrepancy bank (2026-11-11).
        settings.discrepancy_bank_account_id = settings.pettycash_account_id
        fields.append("discrepancy_bank_account")
    settings.save(update_fields=fields)
    return {}


def create_account(user_id, entity_id, payload: dict) -> tuple[dict, int]:
    """Add one account (4.3b, 4.4, 4.5a's "Create a new account"). With ``role``, it is also chosen
    for that daily-closing account."""
    if not has_permission_by_user_id(user_id, Permission.COA_CREATE, entity_id):
        return DENIED, 403
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    role = payload.get("role")
    if role is not None and role not in SLOTS:
        return {"error": "Choose which daily-closing account this is for."}, 400
    clean, fields = _check(entity_id, payload)
    if not fields and role and clean["type"] not in SLOTS[role][1]:
        fields = {"type": "That type can't be used for this account. Choose one from the list."}
    if fields:
        return {"error": next(iter(fields.values())), "fields": fields}, 409 if _is_clash(
            fields
        ) else 400
    with transaction.atomic():
        account = _create(
            user_id,
            entity,
            clean["code"],
            clean["name"],
            clean["type"],
            clean["description"],
            source="user",
        )
        if role:
            _assign(entity_id, role, account)
    logger.info("accounting: entity %s added account %s", entity_id, account.id)
    return {"account": _row(account)}, 201


def _is_clash(fields: dict) -> bool:
    return any("already" in v for v in fields.values())


def _own(entity_id, account_id) -> AccountInfo | None:
    try:
        return _active(entity_id).filter(id=account_id).first()
    except Exception:  # noqa: BLE001 - not a uuid: the same answer as an unknown id
        logger.info("accounting: account id %r is not a uuid", account_id)
        return None


def update_account(user_id, entity_id, account_id, payload: dict) -> tuple[dict, int]:
    """4.4a: one account's code, name, type and description. Accounts from Xero change in Xero."""
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    account = _own(entity_id, account_id)
    if account is None:
        return {"error": "That account isn't in this company. Reload the list and try again."}, 404
    if account.xero_account_id:
        return {"error": "This account comes from Xero. Change it in Xero, and Minty follows."}, 409
    clean, fields = _check(entity_id, payload, exclude=account.id)
    if fields:
        return {"error": next(iter(fields.values())), "fields": fields}, 409 if _is_clash(
            fields
        ) else 400
    with transaction.atomic():
        _apply_update(entity, account, clean)
    return {"account": _row(account)}, 200


def _apply_update(entity: Entity, account: AccountInfo, clean: dict) -> None:
    """One account's checked code, name, type and description, with its old-reader rows; a
    daily-closing slot never keeps an account of the wrong type. The wizard's edit and Settings'
    (``chart``) both run this."""
    previous = account.code
    account.code, account.name, account.description = (
        clean["code"],
        clean["name"],
        clean["description"],
    )
    account.account_type, account.type, account.xero_code = (
        clean["type"],
        XERO_TYPE[clean["type"]],
        clean["code"],
    )
    account.save(update_fields=["code", "name", "description", "account_type", "type", "xero_code"])
    if not account.is_system:
        _old_rows(account, entity, previous_code=previous)
    for column, types in SLOTS.values():
        if account.account_type not in types:
            EntityPettycashSettings.objects.filter(entity_id=entity.id, **{column: account.id}).update(
                **{column: None}
            )


def delete_account(user_id, entity_id, account_id) -> tuple[dict, int]:
    """Untick an account: deleted when nothing uses it, else archived (kept for its records)."""
    if not has_permission_by_user_id(user_id, Permission.COA_DELETE, entity_id):
        return DENIED, 403
    if _entity(entity_id) is None:
        return NOT_FOUND, 404
    account = _own(entity_id, account_id)
    if account is None:
        return {"error": "That account isn't in this company. Reload the list and try again."}, 404
    if account.is_system:
        return {
            "error": "Daily closing needs this account. Choose another one for it instead."
        }, 409
    if account.xero_account_id:
        return {
            "error": "This account comes from Xero. Archive it in Xero, and Minty follows."
        }, 409
    with transaction.atomic():
        archived = _retire(account)
    return {"archived": archived}, 200


def _retire(account: AccountInfo) -> bool:
    """Take an account away: archived when something uses it (kept for its records, off every
    list), else deleted with its old-reader rows. True when archived. The wizard's delete and
    Settings' (``chart``) both run this."""
    if not _used(account.id):
        _remove(account)
        return False
    account.record_status, account.status = "archived", "ARCHIVED"
    account.save(update_fields=["record_status", "status"])
    EntityAccountXero.objects.filter(account_id=account.id).update(is_active=False)
    EntityBillAccountXero.objects.filter(
        entity_id=account.entity_id, account_code=account.code, xero_account_id=""
    ).update(is_active=False, is_deleted=True)
    AccountModule.objects.filter(account_id=account.id).update(is_active=False)
    return True


# ---- X2, X3: which of the Xero chart's accounts Minty uses ------------------------------------

NOT_CONNECTED = {"error": "Connect Xero first, then choose the accounts Minty uses."}


def set_xero_ticks(user_id, entity_id, kind, account_ids) -> tuple[dict, int]:
    """Exactly ``account_ids`` of the company's Xero ``kind`` accounts ticked for Petty Cash: its
    ``account_module`` rows, and for expenses ``entity_account_xero`` too (the apps' old reader)."""
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    if kind not in KINDS:
        return {"error": "Choose which accounts to tick: sales or expense."}, 400
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    if not xero_connected(entity):
        return NOT_CONNECTED, 409
    noun = "sales" if kind == "sales" else "expense"
    wanted = {str(i) for i in account_ids} if isinstance(account_ids, list) else set()
    if not wanted:
        return {"error": f"Tick at least one {noun} account to continue."}, 400
    rows = list(
        _active(entity_id)
        .filter(account_type__in=KINDS[kind], is_system=False)
        .exclude(xero_account_id__isnull=True)
        .exclude(xero_account_id="")
    )
    by_id = {str(a.id): a for a in rows}
    if not wanted <= set(by_id):
        return {"error": f"Pick only {noun} accounts here. Reload the list and try again."}, 400
    with transaction.atomic():
        for account in rows:
            on = str(account.id) in wanted
            module, made = AccountModule.objects.get_or_create(
                account_id=account.id,
                module_code="PETTY_CASH",
                defaults={"entity_id": entity.id, "is_active": on},
            )
            if not made and module.is_active != on:
                module.is_active = on
                if not on:
                    module.is_default = False
                module.save(update_fields=["is_active", "is_default"])
        if kind == "expense":
            # The old wizard's save (xero_lists._tick_codes): the codes, on every expense type it ticks.
            from onboarding.services.xero_lists import _tick_codes

            _tick_codes(entity, [by_id[i].xero_code for i in wanted if by_id[i].xero_code])
    logger.info("xero ticks: entity=%s kind=%s ticked=%s of %s", entity_id, kind, len(wanted), len(rows))
    return setup(user_id, entity_id)


def refresh_from_xero(user_id, entity_id) -> tuple[dict, int]:
    """Refresh from Xero (X2-X4): accounts-api syncs the chart now; the step reads it again."""
    if not has_permission_by_user_id(user_id, Permission.COA_VIEW, entity_id):
        return DENIED, 403
    entity = _entity(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    if not xero_connected(entity):
        return NOT_CONNECTED, 409
    if not xero_client.sync(entity_id):
        return {
            "error": "Xero didn't send your accounts just now. Try Refresh again in a moment."
        }, 502
    return setup(user_id, entity_id)


def set_slot(user_id, entity_id, role, account_id) -> tuple[dict, int]:
    """4.5: choose (or clear, with no account) the account for one of daily closing's five."""
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    if role not in SLOTS:
        return {"error": "Choose which daily-closing account to set."}, 400
    if _entity(entity_id) is None:
        return NOT_FOUND, 404
    account = None
    if account_id:
        account = _own(entity_id, account_id)
        if account is None:
            return {
                "error": "That account isn't in this company. Reload the list and try again."
            }, 404
    fields = _assign(entity_id, role, account)
    if fields:
        return {"error": fields["account_id"], "fields": fields}, 400
    return {"role": role, "account": _row(account) if account else None}, 200


# ---- the shared system rows --------------------------------------------------------------------


def _from_template(user_id, entity: Entity, template: AccountTemplate, **extra) -> AccountInfo:
    """A company account from a template row: its modules from the row's Petty Cash / Bill columns,
    and its daily-closing role (e.g. Cash Sales) chosen when that account is still empty."""
    account = _create(
        user_id,
        entity,
        template.code,
        template.name,
        template.type,
        template.description,
        source="template",
        modules=(bool(template.pettycash_default), bool(template.payment_request_default)),
        **extra,
    )
    role = template.pettycash_role
    if role in SLOTS:
        column, _types = SLOTS[role]
        settings = _settings(entity.id)
        if settings is None or getattr(settings, column) is None:
            _assign(entity.id, role, account)
    return account


def _sales_from_set(user_id, entity: Entity) -> int:
    """The business type's sales accounts (owner, 2026-11-11: Accounting Setup has no sales screen
    any more): the set's ticked sales rows are added, and a Cash Sales row fills daily closing's
    cash-sale account. A changed answer swaps the set - template sales rows nothing uses go first;
    "We bank all sales" clears the cash-sale account so its row can go too."""
    settings = _settings(entity.id)
    set_cash_sale = settings is not None and _active(entity.id).filter(
        id=settings.cash_sale_account_id, source="template").exists()
    if entity.uses_sales_cash is False or set_cash_sale:
        # The set chose that cash-sale account: the new answer chooses again (a person's own stays).
        _assign(entity.id, "cash_sale", None)
    for account in _active(entity.id).filter(source="template", is_system=False,
                                             account_type__in=KINDS["sales"]):
        if not _used(account.id):
            _remove(account)
    added = 0
    for template in _templates(entity):
        if template.business_type is None or template.type not in KINDS["sales"]:
            continue
        if not template.ticked_by_default or _present(entity.id, template):
            continue
        if _clash(entity.id, template.code, template.name):
            logger.warning("accounting: entity %s keeps its own %r over the set's", entity.id, template.name)
            continue
        _from_template(user_id, entity, template)
        added += 1
    return added


def _add_shared_rows(user_id, entity: Entity) -> int:
    """Add the template rows every set shares (Petty Cash, Bank, Amount Due to Director), hidden
    when the template marks them system, and choose each for its daily-closing account if empty."""
    added = 0
    for template in _shared(entity):
        if _present(entity.id, template):
            continue
        if _clash(entity.id, template.code, template.name):
            logger.warning(
                "accounting: entity %s keeps its own %r over the shared row",
                entity.id,
                template.name,
            )
            continue
        _from_template(user_id, entity, template, is_system=bool(template.is_system))
        added += 1
    return added


def payment_accounts(entity: Entity) -> list[AccountInfo]:
    """Payment Settings' list (wizard step 8; owner, 2026-11-11): step 4's expense accounts - a
    Minty company's own (4.4), a Xero company's ticked in X3 (or by the old wizard)."""
    rows = _active(entity.id).filter(account_type__in=KINDS["expense"], is_system=False)
    if minty_on_its_own(entity):
        return list(rows)
    used = _used_ids(entity.id)
    return [a for a in rows.exclude(xero_account_id__isnull=True).exclude(xero_account_id="") if a.id in used]


def minty_on_its_own(entity: Entity) -> bool:
    """The company chose to keep its books in Minty (4.1). An organisation recorded mid-matching
    (Settings Phase 2, 2026-11-11) leaves it Minty's until matching's Finish sets ``'xero'``."""
    return entity.setup_mode == "minty"
