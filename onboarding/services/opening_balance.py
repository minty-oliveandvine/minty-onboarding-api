"""The petty-cash opening balance -- the starting cash in the drawer on day one.

Step 5 of the wizard saves it, with the date reporting starts from, on the company's
``entity_pettycash_settings`` row (``opening_balance`` and ``start_date``). No report is
written: Flask's report wizard reads the two values when the company opens its FIRST report
-- the amount is that report's opening balance and the date its default and earliest date.
Flask's ``blueprints/report/services/shared.py::save_opening_balance`` is the same function
behind the same path.

IT IS AN OPENING BALANCE, NOT A CASH ADDITION

The amount is what was already in the drawer, not money someone put into it, so the first
report opens at it with ``cash_addition`` 0. The response keeps the old shape (``cash_addition``
0, ``adjusted_opening_balance`` = the amount) so the wizard binds to either.

ONCE A REPORT IS POSTED, IT IS TOO LATE

The first report has already taken its opening from these values, so a change would do
nothing; it is refused (409) rather than silently ignored.

A BLANK IS NOT ZERO

A missing, empty or whitespace amount is refused (400), not saved as 0: the wizard skips the
call while the field is blank, so a blank that arrives is a missing amount, and a 0 would open
the first report at nothing without anyone having typed it.

NO SEVEN-DAY WINDOW

A user can pick any past date. Only future dates are refused, and that boundary is Hong Kong
midnight rather than UTC -- otherwise a user in the evening is refused a date they are
already living in.
"""

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q

from core.exceptions import AccessDeniedError, ConflictError, OnboardingValidationError
from core.policy import Permission, has_permission_by_user_id
from shared_models.models import CurrencyInfo, Entity, EntityPettycashSettings, Report

logger = logging.getLogger("minty-onboarding")

#: Rows with this status are drafts; anything else (including NULL) is posted.
DRAFT = "draft"


def _parse_amount(value, decimal_places: int) -> Decimal:
    """The typed amount as a Decimal in the currency's places. A blank (None, "" or
    whitespace), anything unparseable, or finer than the currency allows (JPY takes whole
    amounts), is a 400."""
    if value is None or str(value).strip() == "":
        raise OnboardingValidationError(
            "The opening balance is still empty. Add the amount to continue."
        )
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise OnboardingValidationError("Opening amount must be a number") from None
    if not amount.is_finite():
        raise OnboardingValidationError("Opening amount must be a number")
    if amount.normalize().as_tuple().exponent < -decimal_places:
        raise OnboardingValidationError(
            f"That amount has more decimal places than the currency allows ({decimal_places})."
        )
    return amount.quantize(Decimal(1).scaleb(-decimal_places))


def _parse_date(value):
    """``YYYY-MM-DD`` string or date -> date. Raises on anything else."""
    if value is None or value == "":
        raise OnboardingValidationError("A start date is required")
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            raise OnboardingValidationError(
                "Invalid date format (expected YYYY-MM-DD)"
            ) from None
    return value


def _posted_reports(entity_id: str):
    """Reports that are NOT drafts. NULL status counts as posted."""
    return Report.objects.filter(entity_id=entity_id).filter(
        Q(status__isnull=True) | ~Q(status=DRAFT)
    )


def save_opening_balance(user_id, entity_id: str, start_date, amount) -> dict:
    """Save the company's opening balance and start date on its petty-cash settings row."""
    if not has_permission_by_user_id(user_id, Permission.REPORT_EDIT_OWN, entity_id):
        raise AccessDeniedError("Access denied")

    start = _parse_date(start_date)

    # Hong Kong midnight, not UTC -- see the module header.
    today_local = datetime.now(ZoneInfo(settings.DISPLAY_TIMEZONE)).date()
    if start > today_local:
        raise OnboardingValidationError(
            "Start date cannot be in the future. "
            f"You can only choose dates up to {today_local}."
        )

    # The amount is cash in a currency, and the first report counts it in that currency's
    # denominations (cash_info) - so there is no opening balance without one.
    currency_id = Entity.objects.filter(id=entity_id).values_list("currency_id", flat=True).first()
    currency = CurrencyInfo.objects.filter(id=currency_id).first() if currency_id else None
    if currency is None:
        raise OnboardingValidationError("Set the company's currency first.")

    value = _parse_amount(amount, currency.decimal_places)
    if value < 0:
        raise OnboardingValidationError("Opening amount cannot be negative")

    if _posted_reports(entity_id).exists():
        raise ConflictError(
            "Your first report is already posted, so the opening balance can't change."
        )

    # Step 5 runs before Step 6 (account codes) creates the row, so it may not exist yet.
    row, created = EntityPettycashSettings.objects.get_or_create(entity_id=entity_id)
    row.opening_balance = value
    row.start_date = start
    row.save(update_fields=["opening_balance", "start_date"])

    logger.info(
        "opening balance: entity=%s start=%s amount=%s settings_created=%s",
        entity_id, start, value, created,
    )
    return {
        "opening_date": start.isoformat(),
        "opening_balance": float(value),
        "cash_addition": 0.00,
        "adjusted_opening_balance": float(value),
    }
