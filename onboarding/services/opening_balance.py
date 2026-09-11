"""The petty-cash opening balance -- the starting cash in the drawer on day one.

Ported from Minty's ``blueprints/report/services/shared.py::seed_opening_draft``. It is the
most subtle thing in Group D, and every branch below is load-bearing.

IT IS AN OPENING BALANCE, NOT A CASH ADDITION

The amount the user types goes into ``opening_balance`` with ``cash_addition`` left at 0 --
not the other way round. Recording it as an addition would make the starting cash look like
money someone put INTO the drawer on day one, when it is what was already there. The
adjusted opening equals the amount either way, so day one's closing balance is identical and
every subsequent day (which derives its opening from the previous closing) is unaffected. The
difference is purely what the report SAYS happened.

TWO DIFFERENT KEYS, DEPENDING ON WHETHER ONBOARDING IS OVER

  * While the entity has no POSTED report, the single existing draft IS the onboarding
    opening draft. It is bound by ENTITY ALONE, so a revisit that changes the opening date
    moves that draft's ``transaction_date`` instead of seeding a second one for the new date.
  * Once any report has been posted, onboarding is over, and the key falls back to
    ``(entity, date)`` so this can only ever touch a draft for the exact date requested and
    never disturbs an unrelated day's work in progress.

Both halves matter. Keying on (entity, date) always would leave a stale draft behind every
time the user edited the date; keying on entity always would let this reach into a live
drawer months later.

THE "ALREADY EXISTS" CHECK EXCLUDES DRAFTS, AND THAT IS A BUG FIX

It looks at POSTED reports only. An unfiltered check finds the onboarding draft this very
function created on a previous call and refuses to update it -- Flask's comment records that
this is exactly what "broke the all-set page after the r0 backfill ran".

NO SEVEN-DAY WINDOW

The regular first-report flow constrains the start date; onboarding deliberately does not,
so a user can pick any past date in the current month. Only future dates are refused, and
that boundary is Hong Kong midnight rather than UTC -- otherwise a user in the evening is
refused a date they are already living in.
"""

import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from core.exceptions import (AccessDeniedError, ConflictError,
                             OnboardingValidationError)
from core.policy import Permission, has_permission_by_user_id
from shared_models.models import Report, User

logger = logging.getLogger("minty-onboarding")

#: Rows with this status are drafts; anything else (including NULL) is posted.
DRAFT = "draft"


def _safe_float(value) -> float:
    """Best-effort float, 0.0 on anything unparseable.

    Mirrors Flask's ``safe_float``. Deliberately lenient rather than raising: the field is a
    number the user typed, and the negative check below is the one that actually matters.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
    return Report.objects.filter(company=entity_id).filter(
        Q(status__isnull=True) | ~Q(status=DRAFT)
    )


def seed_opening_draft(user_id, entity_id: str, transaction_date, cash_addition) -> dict:
    """Create or update the entity's onboarding opening draft.

    Returns the payload Flask returns, including ``created`` so the wizard can tell an
    insert from an update.
    """
    if not has_permission_by_user_id(user_id, Permission.REPORT_EDIT_OWN, entity_id):
        raise AccessDeniedError("Access denied")

    tx_date = _parse_date(transaction_date)
    if not tx_date:
        raise OnboardingValidationError("A start date is required")

    # Hong Kong midnight, not UTC -- see the module header.
    today_local = datetime.now(ZoneInfo(settings.DISPLAY_TIMEZONE)).date()
    if tx_date > today_local:
        raise OnboardingValidationError(
            "Start date cannot be in the future. "
            f"You can only choose dates up to {today_local}."
        )

    amount = _safe_float(cash_addition)
    if amount < 0:
        raise OnboardingValidationError("Opening amount cannot be negative")

    # POSTED reports only. Including drafts here finds the draft this function wrote on a
    # previous call and refuses to update it.
    if _posted_reports(entity_id).filter(transaction_date=tx_date).exists():
        raise ConflictError(f"A report for {tx_date} already exists.")

    user = User.objects.filter(id=str(user_id)).first()
    # uploaded_by is an FK to user.USERNAME, not to user.id.
    username = user.username if user else None

    # opening_balance (amount) + cash_addition (0).
    adjusted = amount

    with transaction.atomic():
        onboarding_over = _posted_reports(entity_id).exists()

        if onboarding_over:
            # Exact date only -- never reach into another day's in-progress draft.
            draft = Report.objects.filter(
                company=entity_id, transaction_date=tx_date, status=DRAFT
            ).first()
        else:
            # Entity alone, earliest first: the single draft IS the onboarding one, so
            # changing the date MOVES it rather than leaving a stale row behind.
            draft = (
                Report.objects.filter(company=entity_id, status=DRAFT)
                .order_by("transaction_date")
                .first()
            )

        if draft is not None:
            draft.transaction_date = tx_date
            draft.opening_balance = amount
            draft.cash_addition = 0.0
            draft.adjusted_opening_balance = adjusted
            draft.closing_balance = adjusted
            draft.next_transaction_date = tx_date + timedelta(days=1)
            if username:
                draft.uploaded_by = username
            draft.save(
                update_fields=[
                    "transaction_date", "opening_balance", "cash_addition",
                    "adjusted_opening_balance", "closing_balance",
                    "next_transaction_date", "uploaded_by",
                ]
            )
            created = False
        else:
            draft = Report.objects.create(
                id=str(uuid.uuid4()),
                company=entity_id,
                status=DRAFT,
                transaction_date=tx_date,
                next_transaction_date=tx_date + timedelta(days=1),
                opening_balance=amount,
                cash_addition=0.0,
                adjusted_opening_balance=adjusted,
                closing_balance=adjusted,
                # NOT NULL with no database default. Onboarding records no sales and no
                # deposit, but the insert cannot omit them.
                cash_sales=0.0,
                shop_sales=0.0,
                delivery_sales=0.0,
                total_sales=0.0,
                bank_deposit=0.0,
                expenses=0.0,
                uploaded_by=username,
                # The opening is seeded here, but the user still STARTS their first report
                # at the opening section so they can see and confirm it -- so the section is
                # deliberately not pre-marked complete.
                current_section="opening",
                completed_sections=[],
            )
            created = True

    logger.info(
        "opening balance: entity=%s date=%s amount=%s created=%s draft=%s",
        entity_id, tx_date, amount, created, draft.id,
    )
    return {
        "draft_id": draft.id,
        "transaction_date": tx_date.isoformat(),
        "opening_balance": amount,
        "cash_addition": 0.00,
        "adjusted_opening_balance": adjusted,
        "created": created,
    }
