"""The calls this service makes to Xero and to accounts-api's sync for the wizard's Xero lists
(``onboarding/services/xero_lists.py``; Phase 4 of the Flask retirement, 2026-11-06).

One of the service's outbound modules, beside ``minty_client`` (Flask), ``subscription_client``
and ``xero_tokens`` (the token, Xero's connections). The token comes from accounts-api; nothing
here refreshes one.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from minty_shared import tokens

logger = logging.getLogger("minty-onboarding")

XERO_TIMEOUT = (10, 30)
SYNC_TIMEOUT = (5, 60)


class XeroUnanswered(Exception):
    """Xero did not answer (logged)."""


def _headers(token: str, xero_org_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Xero-Tenant-Id": str(xero_org_id), "Accept": "application/json"}


def get(token: str, xero_org_id, path: str, params: dict) -> dict:
    """``GET {XERO_API_BASE_URL}/{path}``'s JSON. Raises ``XeroUnanswered``."""
    try:
        response = requests.get(f"{settings.XERO_API_BASE_URL}/{path}", headers=_headers(token, xero_org_id),
                                params=params, timeout=XERO_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("xero: %s unreachable for org %s: %s", path, xero_org_id, exc)
        raise XeroUnanswered from None
    if response.status_code != 200:
        logger.error("xero: %s answered %s for org %s: %s", path, response.status_code, xero_org_id,
                     (response.text or "")[:300])
        raise XeroUnanswered
    try:
        return response.json() or {}
    except ValueError:
        logger.error("xero: %s answered non-JSON for org %s", path, xero_org_id)
        raise XeroUnanswered from None


def create_supplier(token: str, xero_org_id, name: str) -> dict:
    """POST one supplier contact; Xero's contact (``ContactID``, ``Name``). Raises ``XeroUnanswered``."""
    try:
        response = requests.post(f"{settings.XERO_API_BASE_URL}/Contacts",
                                 headers={**_headers(token, xero_org_id), "Content-Type": "application/json"},
                                 json={"Contacts": [{"Name": name, "IsCustomer": False, "IsSupplier": True}]},
                                 timeout=XERO_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("xero: contact %r unreachable for org %s: %s", name, xero_org_id, exc)
        raise XeroUnanswered from None
    if response.status_code not in (200, 201):
        logger.error("xero: contact %r refused for org %s: %s %s", name, xero_org_id, response.status_code,
                     (response.text or "")[:300])
        raise XeroUnanswered
    try:
        made = ((response.json() or {}).get("Contacts") or [{}])[0]
    except (ValueError, IndexError):
        made = {}
    if not made.get("ContactID"):
        logger.error("xero: contact %r made for org %s but no ContactID came back", name, xero_org_id)
        raise XeroUnanswered
    return made


def sync(entity_id) -> bool:
    """accounts-api's one sync of the company's accounts and contacts, now. True when it ran."""
    assertion = tokens.service_assertion(settings.SECRET_KEY, "xero-sync", entity_id)
    try:
        response = requests.post(settings.XERO_SYNC_URL, headers=tokens.assertion_headers(assertion),
                                 timeout=SYNC_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("xero: the sync could not be asked for entity %s: %s", entity_id, exc)
        return False
    if response.status_code != 200:
        logger.error("xero: the sync answered %s for entity %s", response.status_code, entity_id)
        return False
    return True
