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
import re

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
    ReportExpense,
)

from core.policy import Permission, has_permission_by_user_id

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

#: Minty's type in Xero's spelling, for the old ``account_info.type`` column the apps still read.
XERO_TYPE = {
    "bank": "BANK",
    "current": "CURRENT",
    "fixed": "FIXED",
    "inventory": "INVENTORY",
    "noncurrent": "NONCURRENT",
    "prepayment": "PREPAYMENT",
    "current_liability": "CURRLIAB",
    "liability": "LIABILITY",
    "term_liability": "TERMLIAB",
    "equity": "EQUITY",
    "sales": "SALES",
    "revenue": "REVENUE",
    "other_income": "OTHERINCOME",
    "direct_costs": "DIRECTCOSTS",
    "expense": "EXPENSE",
    "overheads": "OVERHEADS",
    "depreciation": "DEPRECIATN",
}
#: Where a type's codes start when the company has none of that type yet (Xero's demo chart).
CODE_START = {
    "bank": 90,
    "current": 600,
    "fixed": 700,
    "inventory": 630,
    "noncurrent": 700,
    "prepayment": 620,
    "current_liability": 800,
    "liability": 800,
    "term_liability": 900,
    "equity": 960,
    "sales": 200,
    "revenue": 200,
    "other_income": 260,
    "direct_costs": 300,
    "expense": 400,
    "overheads": 400,
    "depreciation": 416,
}
TYPE_WORDS = {
    "bank": "Bank",
    "current": "Current asset",
    "fixed": "Fixed asset",
    "inventory": "Inventory",
    "noncurrent": "Non-current asset",
    "prepayment": "Prepayment",
    "current_liability": "Current liability",
    "liability": "Liability",
    "term_liability": "Non-current liability",
    "equity": "Equity",
    "sales": "Sales",
    "revenue": "Revenue",
    "other_income": "Other income",
    "direct_costs": "Direct costs",
    "expense": "Expense",
    "overheads": "Overhead",
    "depreciation": "Depreciation",
}
CODE_MAX, NAME_MAX, DESCRIPTION_MAX = (
    10,
    80,
    255,
)  # account_info.name is 80 until CONTRACT widens it
CODE_SHAPE = re.compile(r"^[A-Za-z0-9.\-]+$")
MINTY_MADE = ("template", "user")


# ---- reading -----------------------------------------------------------------------------------


def _entity(entity_id) -> Entity | None:
    return Entity.objects.filter(id=entity_id).first()


def _active(entity_id):
    return AccountInfo.objects.filter(entity_id=entity_id, record_status="active")


def _kind_of(account_type: str | None) -> str | None:
    return next((k for k, types in KINDS.items() if account_type in types), None)


def _row(account: AccountInfo) -> dict:
    return {
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
    """The next free code for a type (4.3b): one past the type's highest number, else where the type
    starts, moved on past any code the company already uses."""
    rows = list(_active(entity_id).values_list("code", "account_type"))
    taken = {c for c, _ in rows if c}
    numbers = [n for c, t in rows if t == account_type and (n := _code_number(c)) is not None]
    number = max(numbers) + 1 if numbers else CODE_START.get(account_type, 100)
    width = 3
    while f"{number:0{width}d}" in taken:
        number += 1
    return f"{number:0{width}d}"


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
    lists = {
        kind: [_row(a) for a in accounts if a.account_type in types and not a.is_system]
        for kind, types in KINDS.items()
    }
    settings = _settings(entity_id)
    by_id = {a.id: a for a in accounts}
    slots = {}
    for role, (column, types) in SLOTS.items():
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
        "connected": bool(entity.xero_org_id),
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
    if (
        mode == "xero"
        and _active(entity_id).filter(source__in=MINTY_MADE, is_system=False).exists()
    ):
        return {
            "error": "This company has its own accounts in Minty. Remove them to connect Xero, "
            "or keep using Minty on its own."
        }, 409
    if entity.setup_mode != mode:
        entity.setup_mode = mode
        entity.save(update_fields=["setup_mode"])
        logger.info("accounting: entity %s set up with %s", entity_id, mode)
    return answers(entity), 200


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
        added = _add_shared_rows(user_id, entity) if entity.setup_mode == "minty" else 0
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
    fields = {}
    if code:
        holder = accounts.filter(code=code).first()
        if holder:
            fields["code"] = (
                f"The code {code} is already used by {holder.name}. Choose another code."
            )
    holder = accounts.filter(name__iexact=name.strip()).first()
    if holder:
        fields["name"] = f"Another account is already called {holder.name}. Choose another name."
    return fields


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
            added.append(
                _create(
                    user_id,
                    entity,
                    template.code,
                    template.name,
                    template.type,
                    template.description,
                    source="template",
                )
            )
    logger.info("accounting: entity %s auto-filled %s %s account(s)", entity_id, len(added), kind)
    return {"added": [_row(a) for a in added]}, 200


def _used(account_id) -> bool:
    settings_columns = [c for c, _t in SLOTS.values()] + ["discrepancy_bank_account_id"]
    return (
        ReportExpense.objects.filter(account_id=account_id).exists()
        or BillLine.objects.filter(account_id=account_id).exists()
        or EntityPettycashSettings.objects.filter(
            Q(**{settings_columns[0]: account_id})
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
    """The cleaned fields and the field sentences for one account's code, name, type, description."""
    code = str(payload.get("code") or "").strip()
    name = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip()
    account_type = payload.get("type")
    description = str(payload.get("description") or "").strip()
    fields = {}
    if account_type not in XERO_TYPE:
        fields["type"] = "Choose the account type to continue."
    if not code and account_type != "bank":
        fields["code"] = "The code is still empty. Add a code to continue."
    elif len(code) > CODE_MAX:
        fields["code"] = f"The code can be up to {CODE_MAX} characters. Shorten it to continue."
    elif code and not CODE_SHAPE.match(code):
        fields["code"] = (
            "The code can use letters, numbers, dots and dashes. Remove the other characters."
        )
    if not name:
        fields["name"] = "The name is still empty. Add a name to continue."
    elif len(name) > NAME_MAX:
        fields["name"] = f"The name can be up to {NAME_MAX} characters. Shorten it to continue."
    if len(description) > DESCRIPTION_MAX:
        fields["description"] = (
            f"The description can be up to {DESCRIPTION_MAX} characters. Shorten it to continue."
        )
    if not fields:
        fields = _clash(entity_id, code or None, name, exclude)
    return {
        "code": code or None,
        "name": name,
        "type": account_type,
        "description": description or None,
    }, fields


def _old_rows(account: AccountInfo, entity: Entity, *, previous_code=None) -> None:
    """Keep the apps' old readers in step with an expense account (Petty Cash's ticks, Payment
    Request's bill codes) and give it its module rows."""
    if account.account_type not in KINDS["expense"]:
        EntityAccountXero.objects.filter(account_id=account.id).delete()
        return
    EntityAccountXero.objects.update_or_create(
        account_id=account.id,
        defaults={"name": account.name, "type": account.type, "is_active": True},
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
            is_active=True,
        )
    else:
        bill.account_code, bill.account_name, bill.account_type = (
            account.code,
            account.name,
            account.type,
        )
        bill.save(update_fields=["account_code", "account_name", "account_type"])
    for module in ("PETTY_CASH", "PAYMENT_REQUEST"):
        AccountModule.objects.get_or_create(
            account_id=account.id,
            module_code=module,
            defaults={"entity_id": entity.id, "is_active": True},
        )


def _create(
    user_id, entity, code, name, account_type, description, *, source, is_system=False
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
        _old_rows(account, entity)
    return account


def _remove(account: AccountInfo) -> None:
    """Delete an account nothing uses, with its old-reader rows."""
    EntityBillAccountXero.objects.filter(
        entity_id=account.entity_id, account_code=account.code, xero_account_id=""
    ).delete()
    EntityAccountXero.objects.filter(account_id=account.id).delete()
    AccountModule.objects.filter(account_id=account.id).delete()
    account.delete()


def _assign(entity_id, role, account: AccountInfo | None) -> dict:
    column, types = SLOTS[role]
    if account is not None and account.account_type not in types:
        return {"account_id": "That account can't be used for this. Choose one from the list."}
    settings, _ = EntityPettycashSettings.objects.get_or_create(entity_id=entity_id)
    setattr(settings, column, account.id if account else None)
    settings.save(update_fields=[column.removesuffix("_id")])
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
    previous = account.code
    with transaction.atomic():
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
        account.save(
            update_fields=["code", "name", "description", "account_type", "type", "xero_code"]
        )
        if not account.is_system:
            _old_rows(account, entity, previous_code=previous)
        for role, (
            column,
            types,
        ) in SLOTS.items():  # a slot never holds an account of the wrong type
            if account.account_type not in types:
                EntityPettycashSettings.objects.filter(
                    entity_id=entity_id, **{column: account.id}
                ).update(**{column: None})
    return {"account": _row(account)}, 200


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
        if _used(account.id):
            account.record_status, account.status = "archived", "ARCHIVED"
            account.save(update_fields=["record_status", "status"])
            EntityAccountXero.objects.filter(account_id=account.id).update(is_active=False)
            EntityBillAccountXero.objects.filter(
                entity_id=entity_id, account_code=account.code, xero_account_id=""
            ).update(is_active=False, is_deleted=True)
            return {"archived": True}, 200
        _remove(account)
    return {"archived": False}, 200


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
        account = _create(
            user_id,
            entity,
            template.code,
            template.name,
            template.type,
            template.description,
            source="template",
            is_system=bool(template.is_system),
        )
        added += 1
        role = template.pettycash_role
        if role in SLOTS:
            column, _types = SLOTS[role]
            settings = _settings(entity.id)
            if settings is None or getattr(settings, column) is None:
                _assign(entity.id, role, account)
    return added


def minty_on_its_own(entity: Entity) -> bool:
    """The company chose to keep its books in Minty without Xero (4.1) and has no Xero org."""
    return entity.setup_mode == "minty" and not entity.xero_org_id
