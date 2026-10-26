"""Group C -- creating and editing the company. Step 1 of the wizard.

``POST /create`` makes the company. ``PUT /entity/<id>`` edits it in place when the user
navigates back to Step 1 -- it NEVER creates, so a stale wizard cannot fork a second company
by re-submitting an edit.

PRESENCE, NOT TRUTHINESS, ON THE UPDATE PATH

The update reads optional fields by whether the KEY is in the payload, not whether the value
is truthy. Both contact fields are optional, so `""` is a meaningful value meaning "clear
this", while an absent key means "leave it alone" -- which is what keeps older callers that
never send them working. Collapsing the two would make it impossible to delete a phone
number.

A SUPPLIED-BUT-UNRESOLVABLE REGISTRY VALUE IS A 400

Not a skipped assignment. Flask's comment records why: skipping left ``country_code`` NULL
while the currency (whose resolver also accepts names) still saved, so the two silently
diverged and the 200 gave the wizard no way to notice.

ASYMMETRY WORTH KNOWING: both derive a currency from the country when none is given, but only
the update sets ``currency_format`` from the currency's symbol. That is Flask's behaviour,
ported as-is rather than tidied. Create also REQUIRES a country and a currency (2026-10-19);
the update does not, so a partial edit without a country stays valid.
"""

import logging

from django.db import transaction
from ninja import Body, Router, Schema, Status

from core.exceptions import AccessDeniedError, ConflictError, OnboardingValidationError
from core.permissions import entity_for_member, require_entity_id
from core.policy import Permission, has_permission_by_user_id
from onboarding.services import resolve
from onboarding.services.cash_settings import sync_cash_settings
from onboarding.services.entity_create import create_entity_for_user
from shared_models.models import CountryInfo, CurrencyInfo, Entity

logger = logging.getLogger("minty-onboarding")

entity_router = Router()

#: Max length of entities.name, on create and rename. The wizard's live check (Step 1 says so
#: as it is typed and holds Save & Next) is a convenience; this is a plain JSON endpoint.
NAME_MAX = 100


class CreateEntityIn(Schema):
    """Every field optional at the schema level so the validators below produce Flask's
    exact sentences rather than ninja's field-name messages.

    The aliases are Flask's: the wizard settled on ``entity_name`` / ``country_id`` /
    ``currency_id``, and the others are older callers still in the wild.
    """

    entity_name: str = ""
    name: str = ""
    country_id: str = ""
    country: str = ""
    country_code: str = ""
    currency_id: str = ""
    currency: str = ""
    currency_code: str = ""
    contact_phone: str = ""
    business_email: str = ""
    # The browser's IANA zone, sent with no field on screen (2026-10-13).
    timezone: str = ""


def _country_currency(country: str) -> str:
    """The country's registry currency, or "" - used when Step 1 names a country alone."""
    if not country:
        return ""
    row = CountryInfo.objects.filter(country_code=country).first()
    return str(row.currency_id) if row is not None and row.currency_id else ""


def _first(*values) -> str:
    """The first non-empty value, mirroring Flask's chain of ``or`` fallbacks."""
    for value in values:
        if value:
            return value
    return ""


def _resolved_country(raw: str) -> str:
    code = resolve.country_code(raw)
    if raw and not code:
        raise OnboardingValidationError(f"Unknown country: {raw}")
    return code


def _resolved_currency(raw: str) -> str:
    cid = resolve.currency_id(raw)
    if raw and not cid:
        raise OnboardingValidationError(f"Unknown currency: {raw}")
    return cid


# response= is REQUIRED for any status other than 200, even with Status(): ninja 1.7
# refuses a status the decorator has no declared schema for, and the refusal surfaces as
# a 500 AFTER the write has already committed. `dict` means "pass the body through".
@entity_router.post("/create", response={201: dict})
def create_entity(request, payload: CreateEntityIn):
    """Create the company (Step 1). Answers 201 with ``{entity_id, name}``.

    Order matters: everything is validated BEFORE the company is created, so a bad phone or
    email -- or a missing country or currency -- is a 400 rather than a half-saved company the
    user then has to find again.

    Country and currency are required (owner, 2026-10-19: Step 1 no longer pre-fills Hong
    Kong). A country alone is enough when the registry gives it a currency.

    201 is also returned when an abandoned in-progress company of the same name is reused --
    see entity_create.create_entity_for_user. From the wizard's point of view the outcome is
    identical: here is your company id.
    """
    country = _resolved_country(
        _first(payload.country_id, payload.country, payload.country_code)
    )
    currency = _resolved_currency(
        _first(payload.currency_id, payload.currency, payload.currency_code)
    ) or _country_currency(country)
    if not country:
        raise OnboardingValidationError("Choose the company's country.")
    if not currency:
        raise OnboardingValidationError("Choose the company's currency.")
    if len(_first(payload.entity_name, payload.name).strip()) > NAME_MAX:
        raise OnboardingValidationError(
            f"Entity name must be {NAME_MAX} characters or fewer."
        )

    phone, phone_error = resolve.contact_phone(payload.contact_phone)
    if phone_error:
        raise OnboardingValidationError(phone_error)
    email, email_error = resolve.business_email(payload.business_email)
    if email_error:
        raise OnboardingValidationError(email_error)
    tz_name, tz_error = resolve.timezone(payload.timezone)
    if tz_error:
        raise OnboardingValidationError(tz_error)

    entity, created = create_entity_for_user(
        request.auth_user_id,
        _first(payload.entity_name, payload.name),
        country,
        currency,
    )

    if created or phone is not None or email is not None:
        # On the reuse path, only overwrite contact details that were actually sent --
        # blanking a stored number because a resumed wizard omitted it would lose data the
        # user entered on a previous visit.
        entity.contact_phone = phone if created or phone is not None else entity.contact_phone
        entity.business_email = (
            email if created or email is not None else entity.business_email
        )
        entity.save(update_fields=["contact_phone", "business_email"])
    if tz_name:
        entity.timezone = tz_name
        entity.save(update_fields=["timezone"])

    # Status(), not a bare tuple: ninja 1.7 deprecated tuple returns and refuses any
    # status the decorator has no declared schema for -- a 201 came back as a 500.
    return Status(201, {"entity_id": entity.id, "name": entity.name})


@entity_router.put("/entity/{entity_id}")
def update_entity(request, entity_id: str, payload: dict = Body(default={})):
    """Edit an in-progress company in place. Answers 200 with ``{entity_id, name}``.

    Takes a raw ``dict`` rather than a Schema on purpose: this endpoint must distinguish an
    ABSENT key from an empty one, and a pydantic Schema with defaults cannot -- an omitted
    ``contact_phone`` and a ``contact_phone`` of ``""`` both arrive as ``""``. The presence
    check is the contract here, so the payload stays untyped and the reads below are explicit
    about which keys they looked for.

    ``Body(...)`` is required with an untyped dict: ninja infers a Schema-typed argument as
    the body but an untyped one as a QUERY parameter, so without it every PUT answered 400
    "payload: Field required".
    """
    entity_id = require_entity_id(entity_id)
    entity = entity_for_member(request.auth_user_id, entity_id)
    payload = payload or {}

    changed: list[str] = []

    # --- Name: a rename is admin-only, re-checked on this crafted PUT ---------------
    if "entity_name" in payload or "name" in payload:
        new_name = str(
            payload.get("entity_name") or payload.get("name") or ""
        ).strip()
        if new_name != (entity.name or ""):
            if not has_permission_by_user_id(
                request.auth_user_id, Permission.ENTITY_RENAME, entity_id
            ):
                # 403 with Flask's wording. Note this is a DIFFERENT gate from membership:
                # a cashier may reach this endpoint and still not be allowed to rename.
                raise AccessDeniedError(
                    "You don't have permission to rename this entity"
                )
            if not new_name:
                raise OnboardingValidationError("Entity name is required.")
            if len(new_name) > NAME_MAX:
                raise OnboardingValidationError(
                    f"Entity name must be {NAME_MAX} characters or fewer."
                )
            if Entity.objects.filter(name=new_name).exclude(id=entity_id).exists():
                raise ConflictError("Entity name already exist")
            entity.name = new_name
            changed.append("name")

    # --- Country, then currency ------------------------------------------------------
    raw_country = _first(
        payload.get("country_id"), payload.get("country"), payload.get("country_code")
    )
    country = _resolved_country(raw_country)
    if country:
        entity.country_code = country
        changed.append("country_code")

    raw_currency = _first(
        payload.get("currency_id"), payload.get("currency"), payload.get("currency_code")
    )
    currency = _resolved_currency(raw_currency)

    # An explicit currency wins; otherwise the country's registry currency is derived. The
    # dropdowns show names but carry these values, and deriving means picking a country
    # alone still leaves the company with a currency.
    if not currency and country:
        currency = _country_currency(country)

    if currency:
        entity.currency_id = currency
        changed.append("currency_id")
        info = CurrencyInfo.objects.filter(id=currency).first()
        if info is not None:
            # "$" when the registry records no symbol -- which, in the dev database, is
            # every row. Flask does the same; a blank currency_format renders as nothing.
            entity.currency_format = info.symbol or "$"
            changed.append("currency_format")

    # --- Contact details: keyed off PRESENCE, so "" clears ---------------------------
    if "contact_phone" in payload:
        phone, error = resolve.contact_phone(payload.get("contact_phone"))
        if error:
            raise OnboardingValidationError(error)
        entity.contact_phone = phone
        changed.append("contact_phone")
    if "business_email" in payload:
        email, error = resolve.business_email(payload.get("business_email"))
        if error:
            raise OnboardingValidationError(error)
        entity.business_email = email
        changed.append("business_email")
    if payload.get("timezone"):
        tz_name, error = resolve.timezone(payload.get("timezone"))
        if error:
            raise OnboardingValidationError(error)
        entity.timezone = tz_name
        changed.append("timezone")

    if changed:
        with transaction.atomic():
            entity.save(update_fields=sorted(set(changed)))
            if currency:
                # The denominations follow the currency; idempotent, so a re-save of the
                # same currency keeps the company's own on/off and order.
                sync_cash_settings(entity.id, currency)
        logger.info("onboarding: entity %s updated (%s)", entity_id, ", ".join(sorted(set(changed))))

    return {"entity_id": entity.id, "name": entity.name}
