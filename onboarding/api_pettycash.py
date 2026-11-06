"""Group D -- petty-cash configuration. SPLIT IN TWO, and the split is the point.

D1 PORTED HERE: ``sales-methods`` and ``opening-balance``. Neither touches Xero --
``payment_methods.py`` in Flask has zero Xero references, and the opening balance is two
columns on the settings row. Both are clean ports.

D2 PORTED 2026-11-06 (Phase 4 of the Flask retirement): ``account-codes``, ``contacts``,
``contacts/create``, ``bill-codes`` (``services/xero_lists.py``). They were proxied to Flask until
then, for the reasons below, which no longer hold: accounts-api is now the one Xero rail (the token
and the one sync), and this service only uses tokens it is given, as minty-payment-request-api does.

WHY D2 IS NOT PORTED

Not because it is hard, but because porting it would put a second Xero integration in a
service whose founding rule is that Flask owns the Xero rail:

  * ``account-codes`` fetches the live chart of accounts from Xero and syncs
    ``account_info`` / ``entity_account_xero`` back. Flask's service is 418 lines and pulls
    in ~990 more -- ``services/helpers/xero_bridge.py``, ``xero/services/settings.py``,
    ``entity/services/xero_account_mapping_post.py``.
  * ``contacts`` and ``contacts/create`` read and create Xero contacts, in the same file.
  * ``bill-codes`` is a third service's territory entirely: it works on
    ``entity_bill_account_xero``, which is minty-payment-request-api's snapshot table, and it imports
    ``sync_xero_coa_bill`` from Flask's 2,530-line ``entity/services/settings.py``.

So roughly 1,400 lines of Xero and bills integration, duplicated in a second language, to be
deleted again when ``xero-service`` is extracted. Proxying costs four thin handlers and keeps
the frontend on ONE base URL, which is the whole reason the proxies exist rather than letting
the wizard call Flask directly for these paths.

These four move when ``xero-service`` does. Until then they sit beside Groups F and G on the
Flask side of the write rule.
"""

from django.http import JsonResponse
from ninja import Body, Router, Schema

from core.permissions import entity_for_member, require_entity_id
from onboarding.services import opening_balance as opening_service
from onboarding.services import sales_methods as sales_service
from onboarding.services import xero_lists

pettycash_router = Router()


class SalesMethodsIn(Schema):
    entity_id: str = ""
    # Typed as lists so a non-list is rejected by the schema; the service re-checks anyway,
    # because it is also called from the proxy-free path in tests.
    electronic: list[str] = []
    delivery: list[str] = []


class OpeningBalanceIn(Schema):
    entity_id: str = ""
    # Flask accepts either key for each value. ``opening_date`` and ``cash_addition`` are
    # what the wizard sends; the other two are older callers.
    opening_date: str = ""
    transaction_date: str = ""
    cash_addition: float | str | None = None
    opening_balance: float | str | None = None


# ---------------------------------------------------------------------------
# D1 -- ported
# ---------------------------------------------------------------------------
@pettycash_router.get("/sales-methods")
def get_sales_methods(request, entity_id: str = ""):
    """Enabled Electronic/Delivery method names, grouped by type."""
    entity_for_member(request.auth_user_id, entity_id)
    return sales_service.list_grouped(request.auth_user_id, entity_id)


@pettycash_router.post("/sales-methods")
def post_sales_methods(request, payload: SalesMethodsIn):
    """Reconcile the entity's methods to the submitted name lists.

    Reconciliation, not replacement: absent names are soft-disabled rather than deleted, and
    matched names keep their catalog link across a rename. See services/sales_methods.py.
    """
    entity_for_member(request.auth_user_id, payload.entity_id)
    return sales_service.replace(
        request.auth_user_id, payload.entity_id, payload.electronic, payload.delivery
    )


@pettycash_router.post("/opening-balance")
def post_opening_balance(request, payload: OpeningBalanceIn):
    """Save the opening balance and start date on the company's petty-cash settings.

    The first report opens at this amount -- cash that was already in the drawer, not money
    added to it. See services/opening_balance.py.
    """
    entity_for_member(request.auth_user_id, payload.entity_id)

    # Flask reads opening_date first, then transaction_date; and cash_addition first, then
    # opening_balance. The order is the contract -- a caller sending both means the first.
    # Neither is None, which the service refuses as empty -- never a silent 0.
    date_value = payload.opening_date or payload.transaction_date
    amount = payload.cash_addition if payload.cash_addition is not None else payload.opening_balance
    return opening_service.save_opening_balance(
        request.auth_user_id, payload.entity_id, date_value, amount
    )


# ---------------------------------------------------------------------------
# D2 -- the Xero lists, done here since Phase 4 of the Flask retirement (2026-11-06);
# onboarding/services/xero_lists.py. Each answer is Flask's (status and body).
# ---------------------------------------------------------------------------
def _answer(result: tuple[dict, int]) -> JsonResponse:
    body, status = result
    return JsonResponse(body, status=status)


def _entity_from(payload) -> str:
    return require_entity_id(payload.get("entity_id") if isinstance(payload, dict) else "")


@pettycash_router.get("/account-codes")
def get_account_codes(request, entity_id: str = ""):
    """Step 5's lists, live from Xero, and the current choices. 409 until Xero is connected."""
    return _answer(xero_lists.account_code_options(request.auth_user_id, require_entity_id(entity_id)))


@pettycash_router.post("/account-codes")
def post_account_codes(request, payload: dict = Body(default={})):
    return _answer(xero_lists.save_account_codes(request.auth_user_id, _entity_from(payload),
                                                 payload.get("expense_codes"), payload.get("mapping")))


@pettycash_router.post("/contacts")
def post_contacts(request, payload: dict = Body(default={})):
    return _answer(xero_lists.save_contacts(request.auth_user_id, _entity_from(payload), payload.get("contacts")))


@pettycash_router.post("/contacts/create")
def post_contacts_create(request, payload: dict = Body(default={})):
    """Creates a contact IN XERO (a supplier), stored so the dropdowns show it at once."""
    name = payload.get("name") or payload.get("contact_name") or ""
    return _answer(xero_lists.create_contact(request.auth_user_id, _entity_from(payload), name))


@pettycash_router.get("/bill-codes")
def get_bill_codes(request, entity_id: str = ""):
    """The bill codes a payment request may use and which are ticked."""
    return _answer(xero_lists.bill_code_options(request.auth_user_id, require_entity_id(entity_id)))


@pettycash_router.post("/bill-codes")
def post_bill_codes(request, payload: dict = Body(default={})):
    return _answer(xero_lists.save_bill_codes(request.auth_user_id, _entity_from(payload),
                                              payload.get("selected_codes"), payload.get("default_code")))
