"""A company's chart of accounts and contacts in Settings: add, edit, delete (owner, 2026-11-12).

The one implementation (owner's choice, 2026-11-12): onboarding's own account rules and side
effects (``accounting``: the checks, the old readers' rows, the module rows, the daily-closing
slots), reached from the hub through accounts-api (``onboarding/internal.py``).

WHO OWNS WHAT. Every row here is the company's own copy, so the company edits and deletes any of
them (owner, 2026-11-12): the ones it made (``source`` ``user``), those from its Xero (``xero``),
and its copies of Minty's starter chart and template contacts (``template`` - Minty's own list,
``account_template``, is never touched). Only the hidden system rows daily closing needs stay out.

WITH OR WITHOUT XERO. A company live on Xero gets every change in Xero first - an account or
contact created there, an edit sent, a delete archived - and nothing changes in Minty when Xero
says no (its sentence is shown). A company keeping its books in Minty, or disconnected, adds and
changes its own Minty rows; a row already linked to Xero waits for a reconnect (its change would
otherwise be lost, or undone by the next sync). Delete is complete: deleted when nothing uses it,
else archived - kept for the records that use it, gone from every list and picker.

DELETED, THEN ADDED AGAIN (owner, 2026-11-12): brought back, never a twin. An account added with
the code, name and type of one the company deleted - or a contact with the name of one - is that
one: Minty's archived row restored (its module and old-reader rows on again), or, when the row went
(nothing used it), the archived Xero copy linked again; set ACTIVE in Xero when it is there. A
delete marks Minty's copy of the Xero record archived at once, so nothing links to it meanwhile.
Offline, a row still linked to Xero waits for a reconnect (it would stay archived there).

NO DUPLICATE CONTACTS. A contact's name is unique in its company among active ones, case and
inner spaces ignored. A live company's new contact links to the Xero contact of that name when one
is stored (``xero_contact``) before anything is created.
"""

from __future__ import annotations

import logging
import re
import uuid

from django.db import transaction
from django.db.models import F, Q
from minty_db.models import (
    AccountInfo,
    AccountModule,
    AccountTemplate,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    XeroAccount,
    XeroContact,
    XeroContactSync,
)
from minty_shared import accounts as rules

from core import xero_client
from core.policy import Permission, has_permission_by_user_id
from core.xero_tokens import access_token_for
from onboarding.services import accounting
from onboarding.services.xero_lists import xero_connected

logger = logging.getLogger("minty-onboarding")

DENIED = {"error": "You don't have permission to change this company's accounts and contacts."}
NOT_FOUND = {"error": "That company isn't here any more. Reload the page and try again."}
ACCOUNT_GONE = {"error": "That account isn't in this company. Reload the list and try again."}
CONTACT_GONE = {"error": "That contact isn't in this company. Reload the list and try again."}
RECONNECT = {
    "error": "Xero needs to be connected again. Reconnect it in Settings, under Integration, then try again."
}
LINKED_OFFLINE = "{name} is in Xero. Reconnect Xero to change it, so Minty and Xero stay the same."
COMES_BACK_OFFLINE = "{name} was archived in Xero. Reconnect Xero to bring it back."
UNANSWERED = {"error": "Xero didn't answer just now. Mind trying again in a moment?"}
CONTACT_MAX = 150

#: The company's to edit and delete: every source - its copies of Minty's too (owner, 2026-11-12).
OWNED = ("user", "xero", "template")


class _Stop(Exception):
    """A refusal on the way: the answer and its status."""

    def __init__(self, answer: dict, status: int):
        super().__init__(answer.get("error"))
        self.answer, self.status = answer, status


def _gate(user_id, entity_id, permission) -> Entity:
    if not has_permission_by_user_id(user_id, permission, entity_id):
        raise _Stop(DENIED, 403)
    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        raise _Stop(NOT_FOUND, 404)
    return entity


def _xero(entity: Entity) -> str | None:
    """A live company's Xero token; None for a company not live on Xero. A live company whose
    token can't be had is refused (the change would miss Xero)."""
    if not xero_connected(entity):
        return None
    token = access_token_for(str(entity.id))
    if token is None:
        raise _Stop(RECONNECT, 409)
    return token


def _send(token, entity: Entity, method: str, path: str, body: dict) -> dict:
    try:
        return xero_client.write(token, entity.xero_org_id, method, path, body)
    except xero_client.XeroRefused as refused:
        raise _Stop({"error": refused.sentence}, 422) from None
    except xero_client.XeroUnanswered:
        raise _Stop(UNANSWERED, 502) from None


def _answer(fn):
    """``(answer, status)`` from a function that may stop on the way."""

    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _Stop as stop:
            return stop.answer, stop.status

    run.__doc__ = fn.__doc__
    run.__name__ = fn.__name__
    return run


# ---- accounts --------------------------------------------------------------------------------------


def _rights(user_id, entity_id) -> dict:
    """What the person may do here - the page draws its buttons from these, never from a role."""
    def may(permission):
        return has_permission_by_user_id(user_id, permission, entity_id)

    return {"add": may(Permission.COA_CREATE), "edit": may(Permission.COA_UPDATE),
            "delete": may(Permission.COA_DELETE)}


def _account_row(account: AccountInfo, used: bool) -> dict:
    row = accounting._row(account, used)
    row["own"] = account.source in OWNED and not account.is_system
    row["in_xero"] = bool(account.xero_account_id)
    return row


def _xero_account(clean: dict) -> dict:
    body = {"Code": clean["code"] or "", "Name": clean["name"], "Type": rules.XERO_TYPE[clean["type"]]}
    if clean["description"]:
        body["Description"] = clean["description"]
    return body


def _own_account(entity_id, account_id) -> AccountInfo:
    try:
        account = accounting._active(entity_id).filter(id=uuid.UUID(str(account_id))).first()
    except ValueError:
        account = None
    if account is None or account.is_system:
        raise _Stop(ACCOUNT_GONE, 404)
    return account


def _minty_offers(entity: Entity) -> list[AccountTemplate]:
    """Minty's accounts the company can still add (owner, 2026-11-12): the starter chart for its
    country - every business type's, one per name - less the hidden system rows and any the company
    already has (by name, or the same code and name)."""
    taken = {_key(n) for n in accounting._active(entity.id).values_list("name", flat=True)}
    offers, seen = [], set()
    for row in (AccountTemplate.objects.filter(Q(country_code__isnull=True) | Q(country_code=entity.country_code))
                .filter(is_system=False).order_by("sort_order", "code", "name")):
        name = _key(row.name)
        if name in seen or name in taken or accounting._present(entity.id, row):
            continue
        seen.add(name)
        offers.append(row)
    return offers


def _offer_row(row: AccountTemplate) -> dict:
    return {"id": str(row.id), "code": row.code or "", "name": row.name, "type": row.type,
            "type_label": rules.TYPE_WORDS.get(row.type, ""), "description": row.description or ""}


def _bring_account_back(user_id, entity: Entity, clean: dict, token) -> AccountInfo | None:
    """The account the company deleted with this code, name and type, brought back; None when there
    is none (the add goes on as a new one)."""
    same = {"code": clean["code"], "name__iexact": clean["name"], "account_type": clean["type"]}
    row = (AccountInfo.objects.filter(entity_id=entity.id, record_status="archived", merged_into__isnull=True,
                                      is_system=False, **same)
           .order_by(F("xero_account_id").desc(nulls_last=True)).first())
    if row is not None:
        if row.xero_account_id:
            if token is None:
                raise _Stop({"error": COMES_BACK_OFFLINE.format(name=row.name)}, 409)
            _send(token, entity, "POST", f"Accounts/{row.xero_account_id}", {"Status": "ACTIVE"})
            _copy_status(XeroAccount, entity, row.xero_account_id, "ACTIVE")
        with transaction.atomic():
            row.record_status, row.status = "active", "ACTIVE"
            row.save(update_fields=["record_status", "status"])
            AccountModule.objects.filter(account_id=row.id).update(is_active=True)
            EntityAccountXero.objects.filter(account_id=row.id).update(is_active=True)
            EntityBillAccountXero.objects.filter(entity_id=entity.id, account_code=row.code, xero_account_id="",
                                                 is_deleted=True).update(is_deleted=False, is_active=True)
            # its own name kept (matched ignoring case); a description typed now is taken
            accounting._apply_update(entity, row, {**clean, "name": row.name})
        logger.info("chart: entity %s brought account %s back", entity.id, row.id)
        return row
    if token is None:
        return None
    held = set(AccountInfo.objects.filter(entity_id=entity.id, xero_account_id__isnull=False)
               .values_list("xero_account_id", flat=True))
    copy = next((c for c in XeroAccount.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id),
                                                       code=clean["code"], xero_type=rules.XERO_TYPE[clean["type"]])
                 .exclude(status="ACTIVE")
                 if _key(c.name) == _key(clean["name"]) and c.xero_guid not in held), None)
    if copy is None:
        return None
    _send(token, entity, "POST", f"Accounts/{copy.xero_guid}", {"Status": "ACTIVE"})
    _copy_status(XeroAccount, entity, copy.xero_guid, "ACTIVE")
    with transaction.atomic():
        row = accounting._create(user_id, entity, clean["code"], clean["name"], clean["type"], clean["description"],
                                 source="user")
        AccountInfo.objects.filter(id=row.id).update(xero_account_id=copy.xero_guid)
        row.xero_account_id = copy.xero_guid
    logger.info("chart: entity %s brought Xero's archived account %s back", entity.id, copy.xero_guid)
    return row


def _copy_status(model, entity: Entity, guid: str, status: str) -> None:
    """Minty's copy of a Xero record (``xero_account`` / ``xero_contact``) follows what we just did
    in Xero, without waiting for the next sync."""
    model.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id), xero_guid=guid).update(
        status=status)


def _checked(entity_id, payload: dict, exclude=None) -> dict:
    clean, fields = accounting._check(entity_id, payload, exclude)
    if fields:
        raise _Stop({"error": next(iter(fields.values())), "fields": fields},
                    409 if accounting._is_clash(fields) else 422)
    return clean


@_answer
def accounts(user_id, entity_id) -> tuple[dict, int]:
    """The active chart (the hidden system rows left out), each with what may be done to it, the
    types, and each type's next free code."""
    entity = _gate(user_id, entity_id, Permission.COA_VIEW)
    rows = [a for a in accounting._active(entity_id).order_by("code", "name") if not a.is_system]
    held = accounting._held(entity_id, [a.id for a in rows])
    taken = list(accounting._active(entity_id).values_list("code", "account_type"))
    return {
        "accounts": [_account_row(a, a.id in held) for a in rows],
        "types": [{"value": t, "label": rules.TYPE_WORDS[t]} for t in rules.XERO_TYPE],
        "next_codes": {t: rules.next_code(taken, t) for t in rules.XERO_TYPE},
        "xero_live": xero_connected(entity),
        "permissions": _rights(user_id, entity_id),
        "minty_accounts": [_offer_row(row) for row in _minty_offers(entity)],
    }, 200


@_answer
def add_account(user_id, entity_id, payload: dict) -> tuple[dict, int]:
    """A new account: the company's own, or - with ``template_id`` - one of Minty's, added as
    Minty's copy (its module and daily-closing defaults, as onboarding's Auto Fill). In Xero first
    when the company is live there."""
    entity = _gate(user_id, entity_id, Permission.COA_CREATE)
    template = None
    if payload.get("template_id"):
        template = next((row for row in _minty_offers(entity) if str(row.id) == str(payload["template_id"])), None)
        if template is None:
            raise _Stop({"error": "That account isn't one of Minty's to add any more. Reload the list and try again."},
                        404)
        payload = {"code": template.code, "name": template.name, "type": template.type,
                   "description": template.description}
    clean = _checked(entity_id, payload)
    token = _xero(entity)
    if template is None:
        back = _bring_account_back(user_id, entity, clean, token)
        if back is not None:
            return {"account": _account_row(back, back.id in accounting._held(entity_id, [back.id])),
                    "message": f"{back.name} is back."}, 201
    xero_id = None
    if token:
        made = _send(token, entity, "PUT", "Accounts", _xero_account(clean))
        xero_id = ((made.get("Accounts") or [{}])[0]).get("AccountID")
        if not xero_id:
            logger.error("chart: Xero made account %r for entity %s but sent no id", clean["name"], entity_id)
            raise _Stop(UNANSWERED, 502)
    with transaction.atomic():
        if template is not None:
            account = accounting._from_template(user_id, entity, template)
        else:
            account = accounting._create(user_id, entity, clean["code"], clean["name"], clean["type"],
                                         clean["description"], source="user")
        if xero_id:
            AccountInfo.objects.filter(id=account.id).update(xero_account_id=xero_id)
            account.xero_account_id = xero_id
    logger.info("chart: entity %s added account %s (in Xero: %s)", entity_id, account.id, bool(xero_id))
    return {"account": _account_row(account, False), "message": f"{account.name} added."}, 201


@_answer
def edit_account(user_id, entity_id, account_id, payload: dict) -> tuple[dict, int]:
    """The company's own account changed - in Xero first when it is linked there."""
    entity = _gate(user_id, entity_id, Permission.COA_UPDATE)
    account = _own_account(entity_id, account_id)
    clean = _checked(entity_id, payload, exclude=account.id)
    if account.xero_account_id:
        token = _xero(entity)
        if token is None:
            raise _Stop({"error": LINKED_OFFLINE.format(name=account.name)}, 409)
        _send(token, entity, "POST", f"Accounts/{account.xero_account_id}", _xero_account(clean))
    with transaction.atomic():
        accounting._apply_update(entity, account, clean)
    used = account.id in accounting._held(entity_id, [account.id])
    return {"account": _account_row(account, used), "message": f"{account.name} saved."}, 200


@_answer
def delete_account(user_id, entity_id, account_id) -> tuple[dict, int]:
    """The company's own account taken away: deleted, or archived when something uses it; archived
    in Xero too when it is linked there."""
    entity = _gate(user_id, entity_id, Permission.COA_DELETE)
    account = _own_account(entity_id, account_id)
    if account.xero_account_id:
        token = _xero(entity)
        if token is None:
            raise _Stop({"error": LINKED_OFFLINE.format(name=account.name)}, 409)
        _send(token, entity, "POST", f"Accounts/{account.xero_account_id}", {"Status": "ARCHIVED"})
        _copy_status(XeroAccount, entity, account.xero_account_id, "ARCHIVED")
    with transaction.atomic():
        archived = accounting._retire(account)
    logger.info("chart: entity %s %s account %s", entity_id, "archived" if archived else "deleted", account.id)
    return {"archived": archived, "message": f"{account.name} deleted."}, 200


# ---- contacts --------------------------------------------------------------------------------------


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().lower()


def _active_contacts(entity_id):
    return XeroContactSync.objects.filter(entity_id=entity_id, record_status="active", merged_into__isnull=True)


def _holder(entity_id, name: str, exclude=None) -> XeroContactSync | None:
    key = _key(name)
    return next((c for c in _active_contacts(entity_id).exclude(id=exclude) if _key(c.name) == key), None)


def _contact_name(payload: dict) -> str:
    name = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip()
    if not name:
        raise _Stop({"error": "The contact's name is still empty. Add a name to continue.",
                     "fields": {"name": "The contact's name is still empty. Add a name to continue."}}, 422)
    if len(name) > CONTACT_MAX:
        sentence = f"The name can be up to {CONTACT_MAX} characters. Shorten it to continue."
        raise _Stop({"error": sentence, "fields": {"name": sentence}}, 422)
    return name


def _taken(entity_id, name: str, exclude=None) -> None:
    holder = _holder(entity_id, name, exclude)
    if holder is not None:
        sentence = f"{holder.name} is already in your contacts. Use that one, or choose another name."
        raise _Stop({"error": sentence, "fields": {"name": sentence}}, 409)


def _contacts_used(entity_id) -> set:
    from minty_db.models import Bill, EntityPettycashSettings, ReportExpense

    used = set(ReportExpense.objects.filter(report__entity_id=entity_id, contact_id__isnull=False)
               .values_list("contact_id", flat=True))
    used |= set(Bill.objects.filter(entity_id=entity_id, contact_id__isnull=False).values_list("contact_id", flat=True))
    slots = ("director_contact_id", "cash_sale_contact_id", "discrepancy_contact_id")
    settings = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(*slots).first()
    return used | {v for v in (settings or {}).values() if v}


def _contact_row(contact: XeroContactSync, used: bool) -> dict:
    # xero_id: how the apps' pickers name a contact (its Xero id, else its own id)
    return {"id": str(contact.id), "name": contact.name, "own": contact.source in OWNED, "source": contact.source,
            "in_xero": bool(contact.xero_contact_id), "xero_id": contact.xero_contact_id or None, "used": used}


def _own_contact(entity_id, contact_id) -> XeroContactSync:
    try:
        contact = _active_contacts(entity_id).filter(id=uuid.UUID(str(contact_id))).first()
    except ValueError:
        contact = None
    if contact is None:
        raise _Stop(CONTACT_GONE, 404)
    return contact


@_answer
def contacts(user_id, entity_id) -> tuple[dict, int]:
    entity = _gate(user_id, entity_id, Permission.COA_VIEW)
    used = _contacts_used(entity_id)
    rows = sorted(_active_contacts(entity_id), key=lambda c: _key(c.name))
    return {"contacts": [_contact_row(c, c.id in used) for c in rows], "xero_live": xero_connected(entity),
            "permissions": _rights(user_id, entity_id)}, 200


def _xero_contact_for(token, entity: Entity, name: str) -> str:
    """The Xero contact of that name the company doesn't hold yet (from the stored copies), else a
    new one made in Xero. Its ContactID."""
    held = set(XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id__isnull=False)
               .values_list("xero_contact_id", flat=True))
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id),
                                                       status="ACTIVE")
                 if _key(c.name) == _key(name) and c.xero_guid not in held), None)
    if copy is not None:
        return copy.xero_guid
    made = _send(token, entity, "POST", "Contacts",
                 {"Contacts": [{"Name": name, "IsCustomer": False, "IsSupplier": True}]})
    xero_id = ((made.get("Contacts") or [{}])[0]).get("ContactID")
    if not xero_id:
        logger.error("chart: Xero made contact %r for entity %s but sent no id", name, entity.id)
        raise _Stop(UNANSWERED, 502)
    return xero_id


def find_or_make_contact(user_id, entity: Entity, name: str, token: str | None) -> tuple[XeroContactSync, str]:
    """The company's contact of that name, else a new one of its own - linked to the Xero contact
    of the name when one is stored, else created in Xero, for a live company.
    The one way a contact is added (Settings, and the pickers' "+ Add" through ``create_contact``).
    ``(contact, how)``: ``back`` (a deleted one brought back), ``found`` or ``made``."""
    back = _bring_contact_back(entity, name, token)
    if back is not None:
        return back, "back"
    holder = _holder(entity.id, name)
    if holder is not None:
        if token and not holder.xero_contact_id:
            # made in Minty while offline: into Xero now, so the picker's answer is usable there
            xero_id = _xero_contact_for(token, entity, holder.name)
            XeroContactSync.objects.filter(id=holder.id).update(xero_contact_id=xero_id,
                                                                xero_org_id=str(entity.xero_org_id))
            holder.xero_contact_id = xero_id
        return holder, "found"
    xero_id = _xero_contact_for(token, entity, name) if token else None
    contact = XeroContactSync.objects.create(
        entity_id=entity.id, name=name, source="user", created_by=user_id, category="expense_contact",
        xero_contact_id=xero_id, xero_org_id=str(entity.xero_org_id) if xero_id else None)
    return contact, "made"


def _bring_contact_back(entity: Entity, name: str, token) -> XeroContactSync | None:
    """The contact the company deleted with this name, brought back: its archived row active again
    (ACTIVE in Xero when linked), or - its row gone - Xero's archived contact of the name linked to a
    new row and set ACTIVE there. None when there is none."""
    key = _key(name)
    if _holder(entity.id, name) is not None:
        return None
    gone = sorted((c for c in XeroContactSync.objects.filter(entity_id=entity.id, record_status="archived",
                                                             merged_into__isnull=True) if _key(c.name) == key),
                  key=lambda c: not c.xero_contact_id)
    if gone:
        row = gone[0]
        if row.xero_contact_id:
            if token is None:
                raise _Stop({"error": COMES_BACK_OFFLINE.format(name=row.name)}, 409)
            _send(token, entity, "POST", "Contacts",
                  {"Contacts": [{"ContactID": row.xero_contact_id, "ContactStatus": "ACTIVE"}]})
            _copy_status(XeroContact, entity, row.xero_contact_id, "ACTIVE")
        XeroContactSync.objects.filter(id=row.id).update(record_status="active")
        row.record_status = "active"
        logger.info("chart: entity %s brought contact %s back", entity.id, row.id)
        return row
    if token is None:
        return None
    held = set(XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id__isnull=False)
               .values_list("xero_contact_id", flat=True))
    copy = next((c for c in XeroContact.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id))
                 .exclude(status="ACTIVE") if _key(c.name) == key and c.xero_guid not in held), None)
    if copy is None:
        return None
    _send(token, entity, "POST", "Contacts", {"Contacts": [{"ContactID": copy.xero_guid, "ContactStatus": "ACTIVE"}]})
    _copy_status(XeroContact, entity, copy.xero_guid, "ACTIVE")
    row = XeroContactSync.objects.create(entity_id=entity.id, name=copy.name[:150], source="xero",
                                         xero_contact_id=copy.xero_guid, xero_org_id=str(entity.xero_org_id),
                                         xero_contact_link_id=copy.id, category="expense_contact")
    logger.info("chart: entity %s brought Xero's archived contact %s back", entity.id, copy.xero_guid)
    return row


@_answer
def add_contact(user_id, entity_id, payload: dict) -> tuple[dict, int]:
    entity = _gate(user_id, entity_id, Permission.COA_CREATE)
    name = _contact_name(payload)
    _taken(entity_id, name)
    contact, how = find_or_make_contact(user_id, entity, name, _xero(entity))
    logger.info("chart: entity %s %s contact %s (in Xero: %s)", entity_id, how, contact.id,
                bool(contact.xero_contact_id))
    said = f"{contact.name} is back." if how == "back" else f"{contact.name} added."
    return {"contact": _contact_row(contact, contact.id in _contacts_used(entity_id)), "message": said}, 201


@_answer
def edit_contact(user_id, entity_id, contact_id, payload: dict) -> tuple[dict, int]:
    entity = _gate(user_id, entity_id, Permission.COA_UPDATE)
    contact = _own_contact(entity_id, contact_id)
    name = _contact_name(payload)
    _taken(entity_id, name, exclude=contact.id)
    if contact.xero_contact_id:
        token = _xero(entity)
        if token is None:
            raise _Stop({"error": LINKED_OFFLINE.format(name=contact.name)}, 409)
        _send(token, entity, "POST", "Contacts", {"Contacts": [{"ContactID": contact.xero_contact_id, "Name": name}]})
    contact.name = name
    contact.save(update_fields=["name"])
    used = contact.id in _contacts_used(entity_id)
    return {"contact": _contact_row(contact, used), "message": f"{name} saved."}, 200


@_answer
def delete_contact(user_id, entity_id, contact_id) -> tuple[dict, int]:
    entity = _gate(user_id, entity_id, Permission.COA_DELETE)
    contact = _own_contact(entity_id, contact_id)
    if contact.xero_contact_id:
        token = _xero(entity)
        if token is None:
            raise _Stop({"error": LINKED_OFFLINE.format(name=contact.name)}, 409)
        _send(token, entity, "POST", "Contacts",
              {"Contacts": [{"ContactID": contact.xero_contact_id, "ContactStatus": "ARCHIVED"}]})
        _copy_status(XeroContact, entity, contact.xero_contact_id, "ARCHIVED")
    archived = contact.id in _contacts_used(entity_id)
    if archived:
        XeroContactSync.objects.filter(id=contact.id).update(record_status="archived")
    else:
        contact.delete()
    logger.info("chart: entity %s %s contact %s", entity_id, "archived" if archived else "deleted", contact_id)
    return {"archived": archived, "message": f"{contact.name} deleted."}, 200
