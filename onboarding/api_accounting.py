"""Accounting Setup - wizard step 4 with Xero optional (Figma 348:1007; services/accounting.py).

Every route names its company and passes the membership door first (``entity_for_member``); the
service then checks the role permission (``COA_*``), as ``xero_lists`` does. One read draws the
whole step (``GET /accounting``); each change answers what the screen needs to redraw.
"""

from django.db import transaction
from django.http import JsonResponse
from ninja import Body, Router

from core.permissions import entity_for_member, require_entity_id
from onboarding.services import accounting

accounting_router = Router()


def _answer(result: tuple[dict, int]) -> JsonResponse:
    body, status = result
    return JsonResponse(body, status=status)


def _changed(entity_id, change) -> JsonResponse:
    """Run a step-4 change in one transaction. (Until 2026-11-11 it also re-linked the sales methods
    to the sales categories; step 5's methods are free lists again, owner.)"""
    with transaction.atomic():
        body, status = change()
    return JsonResponse(body, status=status)


def _entity(request, payload) -> str:
    entity_id = require_entity_id(payload.get("entity_id") if isinstance(payload, dict) else "")
    entity_for_member(request.auth_user_id, entity_id)
    return entity_id


@accounting_router.get("/accounting")
def get_accounting(request, entity_id: str = ""):
    """The step's answers, both account lists, the five daily-closing accounts, the next codes."""
    entity_for_member(request.auth_user_id, entity_id)
    return _answer(accounting.setup(request.auth_user_id, entity_id))


@accounting_router.post("/accounting/mode")
def post_mode(request, payload: dict = Body(default={})):
    """4.1 - ``{entity_id, mode: "minty" | "xero"}``."""
    entity_id = _entity(request, payload)
    return _answer(accounting.set_mode(request.auth_user_id, entity_id, payload.get("mode")))


@accounting_router.post("/accounting/business")
def post_business(request, payload: dict = Body(default={})):
    """4.2 - ``{entity_id, business_type: "fnb" | "retail" | "service" | "general" | null,
    uses_sales_cash: bool}`` (null business type = "None of these"; "Not sure" sends true)."""
    entity_id = _entity(request, payload)
    return _changed(
        entity_id,
        lambda: accounting.set_business(
            request.auth_user_id,
            entity_id,
            payload.get("business_type"),
            payload.get("uses_sales_cash"),
        ),
    )


@accounting_router.get("/accounting/autofill")
def get_autofill(request, entity_id: str = "", kind: str = ""):
    """What Auto Fill offers for ``kind`` (``sales`` or ``expense``)."""
    entity_for_member(request.auth_user_id, entity_id)
    return _answer(accounting.autofill_options(request.auth_user_id, entity_id, kind))


@accounting_router.post("/accounting/autofill")
def post_autofill(request, payload: dict = Body(default={})):
    """``{entity_id, kind, template_ids: [...]}`` - add the chosen suggestions."""
    entity_id = _entity(request, payload)
    return _changed(
        entity_id,
        lambda: accounting.apply_autofill(
            request.auth_user_id, entity_id, payload.get("kind"), payload.get("template_ids")
        ),
    )


@accounting_router.post("/accounting/autofill/revert")
def post_revert(request, payload: dict = Body(default={})):
    """``{entity_id, kind}`` - take one list's Auto Fill rows back out (the used ones stay)."""
    entity_id = _entity(request, payload)
    return _changed(
        entity_id, lambda: accounting.revert(request.auth_user_id, entity_id, payload.get("kind"))
    )


@accounting_router.post("/accounting/accounts")
def post_account(request, payload: dict = Body(default={})):
    """``{entity_id, code, name, type, description?, role?}`` - add an account (201)."""
    entity_id = _entity(request, payload)
    return _changed(
        entity_id, lambda: accounting.create_account(request.auth_user_id, entity_id, payload)
    )


@accounting_router.patch("/accounting/accounts/{account_id}")
def patch_account(request, account_id: str, payload: dict = Body(default={})):
    """``{entity_id, code, name, type, description?}`` - edit one account."""
    entity_id = _entity(request, payload)
    return _changed(
        entity_id,
        lambda: accounting.update_account(request.auth_user_id, entity_id, account_id, payload),
    )


@accounting_router.delete("/accounting/accounts/{account_id}")
def delete_account(request, account_id: str, entity_id: str = ""):
    """Untick one account: deleted if nothing uses it, else archived."""
    entity_for_member(request.auth_user_id, entity_id)
    return _changed(
        entity_id, lambda: accounting.delete_account(request.auth_user_id, entity_id, account_id)
    )


@accounting_router.post("/accounting/xero-accounts")
def post_xero_accounts(request, payload: dict = Body(default={})):
    """X2, X3 - ``{entity_id, kind: "sales" | "expense", account_ids}``: exactly these of the
    Xero chart's accounts are the ones Minty uses. Answers the whole step again."""
    entity_id = _entity(request, payload)
    return _changed(
        entity_id,
        lambda: accounting.set_xero_ticks(
            request.auth_user_id, entity_id, payload.get("kind"), payload.get("account_ids")
        ),
    )


@accounting_router.post("/accounting/xero-refresh")
def post_xero_refresh(request, payload: dict = Body(default={})):
    """Refresh from Xero - ``{entity_id}``: the chart synced now, then the whole step again."""
    entity_id = _entity(request, payload)
    return _changed(entity_id, lambda: accounting.refresh_from_xero(request.auth_user_id, entity_id))


@accounting_router.post("/accounting/slots")
def post_slot(request, payload: dict = Body(default={})):
    """``{entity_id, role, account_id | null}`` - choose (or clear) a daily-closing account."""
    entity_id = _entity(request, payload)
    return _changed(
        entity_id,
        lambda: accounting.set_slot(
            request.auth_user_id, entity_id, payload.get("role"), payload.get("account_id")
        ),
    )
