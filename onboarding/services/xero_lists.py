"""The wizard's Xero lists: account codes and mapping (step 5), contacts (step 6), a new contact,
and bill codes (step 8).

Ported from Flask in Phase 4 of the Flask retirement (2026-11-06). These answered by forwarding to
Flask's ``/api/onboarding/{account-codes,contacts,contacts/create,bill-codes}``; the Flask sources
are ``entity/services/onboarding_account_codes.py`` and ``onboarding_bill_codes.py``.

The answers, statuses and sentences are Flask's. Each starts with Flask's permission check:
``COA_VIEW`` to read, ``COA_UPDATE`` to save, ``CONTACT_CREATE`` for a new contact; refused is
403 ``{"error": "Access denied"}``.

**Xero.** This service never refreshes a token. It asks accounts-api for one
(``core/xero_tokens.py``) and calls Xero with it (``core/xero_client.py``), as
minty-payment-request-api does. No token means
Flask's 409 ``{error, connected: false}``.
- The account-codes read asks Xero live, as Flask's does. Its lists drop Xero's system accounts,
  which only Xero's answer marks.
- A save resolves each choice to the company's stored row. When one isn't stored yet, the save asks
  accounts-api's one sync (``POST /api/internal/xero/sync``) and looks again. Flask fetched the
  single account or contact from Xero instead.

Deliberate differences from Flask:
- Xero failing to answer the read is a 502 with a sentence. Flask showed empty lists.
- A choice still unresolved after the sync is a 409 naming it. Flask silently kept the old value.
- An onboarding token is required: the route's auth checks the scope, where Flask checked none.
"""

from __future__ import annotations

import logging

from django.db import transaction
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    XeroContactSync,
)

from core import xero_client
from core.policy import Permission, has_permission_by_user_id
from core.xero_tokens import access_token_for

logger = logging.getLogger("minty-onboarding")

DENIED = {"error": "Access denied"}
NOT_FOUND = {"error": "Entity not found"}
CONTACTS_PAGE = 1000

# Flask's lists (the Settings mapping's own): Owners is liability-only and drops system accounts.
OWNERS_TYPES = ("CURRENT", "CURRLIAB", "NONCURRENT", "TERMLIAB", "LIABILITY")
CASHSALE_TYPES = ("SALES", "REVENUE", "INCOME")
DISCREPANCY_TYPES = ("EXPENSE", "DIRECTCOSTS")
#: Petty Cash's code list (Flask's ``COA_INCLUDED_TYPES``).
COA_INCLUDED_TYPES = ("DIRECTCOSTS", "EXPENSE", "OVERHEADS", "PREPAYMENT")

ACCOUNT_FIELDS = (("pettycash", "pettycash_account_id"), ("deposit", "bank_account_id"),
                  ("director", "director_account_id"), ("cash_sale", "cash_sale_account_id"),
                  ("discrepancy", "discrepancy_account_id"))
CONTACT_FIELDS = (("director", "director_contact_id"), ("cash_sale", "cash_sale_contact_id"),
                  ("discrepancy", "discrepancy_contact_id"))


def _not_connected(what: str) -> tuple[dict, int]:
    return {"error": f"Connect to Xero first to {what}.", "connected": False}, 409


def _connection(entity_id) -> tuple[Entity | None, str | None]:
    """The company and a working access token (from accounts-api), or ``(entity, None)``."""
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None or not entity.xero_org_id:
        return entity, None
    return entity, access_token_for(entity_id)


def _contacts(token: str, entity: Entity) -> list[dict]:
    found: list[dict] = []
    page = 1
    while True:
        batch = xero_client.get(token, entity.xero_org_id, "Contacts",
                                {"page": page, "pageSize": CONTACTS_PAGE, "order": "Name ASC"}).get("Contacts") or []
        found.extend(batch)
        if len(batch) < CONTACTS_PAGE:
            return found
        page += 1


# ---- step 5: the account codes and the mapping -----------------------------------------------


def _is_system(account: dict) -> bool:
    flag = account.get("SystemAccount")
    return isinstance(flag, str) and flag.strip() != ""


def _code_key(code) -> tuple:
    text = str(code or "").strip()
    try:
        return (0, int(text), "")
    except ValueError:
        return (1, 0, text.lower())


def _label(account: dict) -> str:
    code = str(account.get("Code") or "").strip()
    name = account.get("Name") or ""
    return f"{code} · {name}" if code else name


def _bank_label(account: dict) -> str:
    """Flask's: the name and the masked account number."""
    name = account.get("Name") or ""
    number = account.get("BankAccountNumber")
    return f"{name} - ****{str(number)[-4:]}" if number else name


def account_code_options(user_id, entity_id) -> tuple[dict, int]:
    """Step 5's lists (live from Xero) and the company's current choices."""
    if not has_permission_by_user_id(user_id, Permission.COA_VIEW, entity_id):
        return DENIED, 403
    entity, token = _connection(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    if token is None:
        return _not_connected("load account codes")
    try:
        accounts = xero_client.get(token, entity.xero_org_id, "Accounts",
                                   {"where": 'Status=="ACTIVE"', "order": "Code"}).get("Accounts") or []
        contacts = _contacts(token, entity)
    except xero_client.XeroUnanswered:
        return {"error": "Xero didn't send your accounts just now. Try again in a moment."}, 502

    def options(types, *, bank=False, drop_system=False):
        rows = sorted((a for a in accounts if a.get("Type") in types and a.get("AccountID")
                       and not (drop_system and _is_system(a))), key=lambda a: _code_key(a.get("Code")))
        return [{"id": a["AccountID"], "label": (_bank_label if bank else _label)(a)} for a in rows]

    expense_codes = sorted(
        ({"code": str(a.get("Code")).strip(), "name": a.get("Name") or ""} for a in accounts
         if a.get("Type") in COA_INCLUDED_TYPES and not _is_system(a) and str(a.get("Code") or "").strip()),
        key=lambda e: _code_key(e["code"]))
    listed = EntityAccountXero.objects.filter(account__entity_id=entity_id, account__type__in=COA_INCLUDED_TYPES)
    selected_codes = [c for c in listed.filter(is_active=True).values_list("account__xero_code", flat=True) if c]
    default_all = not selected_codes and not listed.filter(is_active=False).exists()

    seen, contact_options = set(), []
    for contact in contacts:
        contact_id = contact.get("ContactID")
        if contact_id and contact_id not in seen:
            seen.add(contact_id)
            contact_options.append({"id": contact_id, "label": contact.get("Name") or ""})

    row = EntityPettycashSettings.objects.filter(entity_id=entity_id).first()

    def xero_account(column):
        value = getattr(row, column, None) if row is not None else None
        return AccountInfo.objects.filter(id=value).values_list("xero_account_id", flat=True).first() if value else None

    def xero_contact(column):
        value = getattr(row, column, None) if row is not None else None
        return XeroContactSync.objects.filter(id=value).values_list("xero_contact_id", flat=True).first() if value else None

    return {
        "connected": True,
        "bank_accounts": options(("BANK",), bank=True),
        "cash_sale_accounts": options(CASHSALE_TYPES),
        "director_accounts": options(OWNERS_TYPES, drop_system=True),
        "discrepancy_accounts": options(DISCREPANCY_TYPES),
        "expense_codes": expense_codes,
        "selected_codes": selected_codes,
        "default_all": default_all,
        "mapping_defaults": {key: xero_account(column) for key, column in ACCOUNT_FIELDS},
        "contacts": contact_options,
        "contact_defaults": {key: xero_contact(column) for key, column in CONTACT_FIELDS},
    }, 200


def _resolve(entity_id, wanted: dict[str, str], find) -> tuple[dict, list[str]]:
    """``{key: row id}`` for each wanted Xero id, asking the sync once for any not stored yet."""
    found = {key: find(value) for key, value in wanted.items()}
    if any(v is None for v in found.values()) and xero_client.sync(entity_id):
        found = {key: v if v is not None else find(wanted[key]) for key, v in found.items()}
    return found, [f"{key} ({wanted[key]})" for key, v in found.items() if v is None]


def _apply(row, fields, chosen: dict, resolved: dict) -> None:
    """Flask's clear-on-revisit: a key absent leaves the column; present and empty clears it."""
    for key, column in fields:
        if key not in chosen:
            continue
        setattr(row, column, resolved.get(key) if (chosen.get(key) or "").strip() else None)


def _tick_codes(entity: Entity, codes) -> None:
    """Exactly ``codes`` ticked for Petty Cash (Flask's ``sync_entity_account_xero_active``)."""
    wanted = {str(c).strip() for c in codes if c and str(c).strip()}
    eligible = list(AccountInfo.objects.filter(entity_id=entity.id, type__in=COA_INCLUDED_TYPES))
    selected = {a.id for a in eligible if a.xero_code and a.xero_code.strip() in wanted}
    existing = {e.account_id: e for e in EntityAccountXero.objects.filter(account__entity_id=entity.id)}
    by_id = {a.id: a for a in eligible}
    for row in existing.values():
        row.is_active = row.account_id in selected
        account = by_id.get(row.account_id)
        if account is not None:
            row.name, row.type, row.xero_account_id = account.name, account.type, account.xero_account_id
            row.xero_org_id = entity.xero_org_id
        row.save()
    for account in eligible:
        if account.id not in existing:
            EntityAccountXero.objects.create(account_id=account.id, name=account.name, type=account.type,
                                             xero_org_id=entity.xero_org_id, xero_account_id=account.xero_account_id,
                                             is_active=account.id in selected)


def save_account_codes(user_id, entity_id, expense_codes, mapping) -> tuple[dict, int]:
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    if not isinstance(expense_codes, list):
        return {"error": "expense_codes must be an array"}, 400
    has_codes = AccountInfo.objects.filter(entity_id=entity_id, type__in=COA_INCLUDED_TYPES).exclude(
        xero_code__isnull=True).exclude(xero_code="").exists()
    if not any(str(c).strip() for c in expense_codes if c) and has_codes:
        return {"error": "Pick at least one account code."}, 400
    mapping = mapping if isinstance(mapping, dict) else {}
    petty, deposit = (mapping.get("pettycash") or "").strip(), (mapping.get("deposit") or "").strip()
    if petty and deposit and petty == deposit:
        return {"error": "Petty Cash Account and Deposit Bank Account cannot be the same."}, 400
    entity, token = _connection(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    if token is None:
        return _not_connected("save account codes")

    wanted = {k: (mapping.get(k) or "").strip() for k, _ in ACCOUNT_FIELDS if (mapping.get(k) or "").strip()}

    def find(xero_id):
        return AccountInfo.objects.filter(entity_id=entity_id, xero_account_id=xero_id).values_list("id", flat=True).first()

    # The ticks are on stored rows, and Flask refreshed the accounts on every save: the sync does,
    # so a choice from the live list is stored by the time it is looked up.
    if not xero_client.sync(entity_id):
        logger.warning("save_account_codes: the accounts were not refreshed for entity %s", entity_id)
    resolved = {key: find(value) for key, value in wanted.items()}
    unresolved = [f"{key} ({wanted[key]})" for key, value in resolved.items() if value is None]
    if unresolved:
        return {"error": "Some choices haven't synced from Xero yet: " + "; ".join(unresolved)
                         + ". Try again in a moment."}, 409
    with transaction.atomic():
        row, _ = EntityPettycashSettings.objects.get_or_create(entity_id=entity_id)
        _apply(row, ACCOUNT_FIELDS, mapping, resolved)
        # No discrepancy-bank field here: it mirrors the petty cash bank, and goes when it does.
        if row.pettycash_account_id:
            row.discrepancy_bank_account_id = row.pettycash_account_id
        elif "pettycash" in mapping and not petty:
            row.discrepancy_bank_account_id = None
        row.save()
        _tick_codes(entity, expense_codes)
    logger.info("save_account_codes: entity=%s codes=%s set=%s", entity_id, len(expense_codes), sorted(wanted))
    return {"ok": True, "expense_codes": expense_codes}, 200


# ---- step 6: the contacts ---------------------------------------------------------------------


def save_contacts(user_id, entity_id, contacts) -> tuple[dict, int]:
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    contacts = contacts if isinstance(contacts, dict) else {}
    entity, token = _connection(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    if token is None:
        return _not_connected("save contacts")
    wanted = {k: (contacts.get(k) or "").strip() for k, _ in CONTACT_FIELDS if (contacts.get(k) or "").strip()}

    def find(xero_id):
        return XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=xero_id).values_list("id", flat=True).first()

    resolved, unresolved = _resolve(entity_id, wanted, find)
    if unresolved:
        return {"error": "Some contacts haven't synced from Xero yet: " + "; ".join(unresolved)
                         + ". Try again in a moment."}, 409
    with transaction.atomic():
        row, _ = EntityPettycashSettings.objects.get_or_create(entity_id=entity_id)
        _apply(row, CONTACT_FIELDS, contacts, resolved)
        row.save()
    logger.info("save_contacts: entity=%s set=%s", entity_id, sorted(wanted))
    return {"ok": True}, 200


def create_contact(user_id, entity_id, name) -> tuple[dict, int]:
    """A new supplier in the company's Xero, stored in ``xero_contact_sync`` so it shows at once."""
    if not has_permission_by_user_id(user_id, Permission.CONTACT_CREATE, entity_id):
        return DENIED, 403
    name = (name or "").strip()
    if not name:
        return {"error": "Contact name is required."}, 400
    entity, token = _connection(entity_id)
    if entity is None:
        return NOT_FOUND, 404
    if token is None:
        return _not_connected("create contacts")
    try:
        made = xero_client.create_supplier(token, entity.xero_org_id, name)
    except xero_client.XeroUnanswered:
        return {"error": "Failed to create contact in Xero. Please try again."}, 502
    contact_id = made["ContactID"]
    if not XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=contact_id).exists():
        XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=contact_id, xero_org_id=entity.xero_org_id,
                                       name=(made.get("Name") or name)[:150], category="expense_contact")
    logger.info("create_contact: entity=%s made contact %s", entity_id, contact_id)
    return {"id": contact_id, "label": name}, 201


# ---- step 8: the bill codes -------------------------------------------------------------------


def bill_code_options(user_id, entity_id) -> tuple[dict, int]:
    if not has_permission_by_user_id(user_id, Permission.COA_VIEW, entity_id):
        return DENIED, 403
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        return NOT_FOUND, 404
    on_its_own = entity.setup_mode == "minty" and not entity.xero_org_id  # its own expense accounts
    if not entity.xero_org_id and not on_its_own:
        return _not_connected("load payment account codes")
    rows = EntityBillAccountXero.objects.filter(entity_id=entity_id, is_deleted=False)
    if not rows.exists() and not on_its_own and xero_client.sync(entity_id):  # the connect fills it; the sync mirrors it too
        rows = EntityBillAccountXero.objects.filter(entity_id=entity_id, is_deleted=False)
    codes, selected, inactive, default = [], [], 0, ""
    for row in rows:
        code = (row.account_code or "").strip()
        if not code:
            continue
        if row.is_default:
            default = code
        codes.append({"code": code, "name": row.account_name or ""})
        if row.is_active:
            selected.append(code)
        else:
            inactive += 1
    codes.sort(key=lambda e: _code_key(e["code"]))
    return {"connected": bool(entity.xero_org_id), "bill_codes": codes, "selected_codes": selected,
            "default_all": not selected and inactive == 0, "default_code": default}, 200


def save_bill_codes(user_id, entity_id, selected_codes, default_code=None) -> tuple[dict, int]:
    if not has_permission_by_user_id(user_id, Permission.COA_UPDATE, entity_id):
        return DENIED, 403
    if not isinstance(selected_codes, list):
        return {"error": "selected_codes must be an array"}, 400
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        return NOT_FOUND, 404
    if not entity.xero_org_id and entity.setup_mode != "minty":
        return _not_connected("save payment account codes")
    selected = sorted({str(c).strip() for c in selected_codes if str(c).strip()})
    rows = EntityBillAccountXero.objects.filter(entity_id=entity_id, is_deleted=False)
    listed = {(r or "").strip() for r in rows.values_list("account_code", flat=True)} - {""}
    if listed and not listed & set(selected):
        return {"error": "Pick at least one account code."}, 400
    default = str(default_code).strip() if default_code is not None else None
    if default and default not in selected:
        return {"error": "The default account code must be one of the ticked codes."}, 400
    with transaction.atomic():
        for row in rows.select_for_update():
            code = (row.account_code or "").strip()
            row.is_active = code in selected
            fields = ["is_active"]
            if default is not None:
                row.is_default = code == default
                fields.append("is_default")
            row.save(update_fields=fields)
    logger.info("save_bill_codes: entity=%s selected=%s", entity_id, len(selected))
    return {"ok": True, "selected_codes": selected, "default_code": default}, 200

