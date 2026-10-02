"""Group D -- petty-cash configuration. SPLIT IN TWO, and the split is the point.

D1 PORTED HERE: ``sales-methods`` and ``opening-balance``. Neither touches Xero --
``payment_methods.py`` in Flask has zero Xero references, and the opening draft is a plain
row. Both are clean ports.

D2 PROXIED TO FLASK: ``account-codes``, ``contacts``, ``contacts/create``, ``bill-codes``.

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

from ninja import Body, Router, Schema

from core import minty_client
from core.permissions import entity_for_member, require_entity_id

from onboarding.services import opening_balance as opening_service
from onboarding.services import sales_methods as sales_service


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
    """Seed or move the onboarding opening draft.

    The amount lands in ``opening_balance`` with ``cash_addition`` at 0 -- it is cash that was
    already in the drawer, not money added to it. See services/opening_balance.py.
    """
    entity_for_member(request.auth_user_id, payload.entity_id)

    # Flask reads opening_date first, then transaction_date; and cash_addition first, then
    # opening_balance. The order is the contract -- a caller sending both means the first.
    date_value = payload.opening_date or payload.transaction_date
    amount = (
        payload.cash_addition
        if payload.cash_addition is not None
        else (payload.opening_balance if payload.opening_balance is not None else 0)
    )
    return opening_service.seed_opening_draft(
        request.auth_user_id, payload.entity_id, date_value, amount
    )


# ---------------------------------------------------------------------------
# D2 -- proxied to Flask. Xero and bills territory; see the module header.
# ---------------------------------------------------------------------------
@pettycash_router.get("/account-codes")
def get_account_codes(request, entity_id: str = ""):
    """Xero-sourced account options plus current selections. 409 until Xero is connected."""
    entity_id = require_entity_id(entity_id)
    return minty_client.proxy(
        request, "/api/onboarding/account-codes", method="GET",
        params={"entity_id": entity_id},
    )


@pettycash_router.post("/account-codes")
def post_account_codes(request, payload: dict = Body(default={})):
    return minty_client.proxy(request, "/api/onboarding/account-codes", json=payload)


@pettycash_router.post("/contacts")
def post_contacts(request, payload: dict = Body(default={})):
    return minty_client.proxy(request, "/api/onboarding/contacts", json=payload)


@pettycash_router.post("/contacts/create")
def post_contacts_create(request, payload: dict = Body(default={})):
    """Creates a contact IN XERO. The clearest case for the proxy in the whole group."""
    return minty_client.proxy(request, "/api/onboarding/contacts/create", json=payload)


@pettycash_router.get("/bill-codes")
def get_bill_codes(request, entity_id: str = ""):
    """Bill chart-of-accounts tick state, from minty-payment-request-api's snapshot table."""
    entity_id = require_entity_id(entity_id)
    return minty_client.proxy(
        request, "/api/onboarding/bill-codes", method="GET",
        params={"entity_id": entity_id},
    )


@pettycash_router.post("/bill-codes")
def post_bill_codes(request, payload: dict = Body(default={})):
    return minty_client.proxy(request, "/api/onboarding/bill-codes", json=payload)
