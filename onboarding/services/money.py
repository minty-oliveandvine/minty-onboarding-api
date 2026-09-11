"""Currency formatting, resolved from ``currency_info`` and never hardcoded.

Ported from Minty's blueprints/subscription/services/money.py, keeping three rules that
each exist because they were once broken:

1. THE SYMBOL COMES FROM THE DATABASE. Flask's docstring is explicit that it is "never
   hardcoded", because a hardcoded "HK$" is wrong the moment a second currency is sold.
2. DECIMAL PLACES COME FROM THE DATABASE TOO. HKD 28000 minor units is 280.00; JPY 280
   minor units is 280. Dividing by a fixed 100 silently multiplies a yen price by a
   hundred.
3. A LOOKUP FAILURE MUST NOT COST THE AMOUNT. A missing symbol falls back to the
   upper-cased code, a missing decimal_places to 2. Returning nothing, or raising, would
   mean a pricing page with no prices on it -- strictly worse than "HKD 400.00".

Difference from Flask: it caches per request in ``flask.g``. Django has no equivalent, so
:func:`load_currencies` loads the table once per call and the caller threads the result
through. With three plans on the pricing screen that is one query either way, and an
explicit argument beats a hidden request-global.
"""

import logging
from decimal import Decimal

from shared_models.models import CurrencyInfo

logger = logging.getLogger("minty-onboarding")

#: Used when the code is missing, unknown, or the table cannot be read.
FALLBACK_DECIMAL_PLACES = 2


def load_currencies() -> dict:
    """``{CODE: CurrencyInfo}`` for every currency, upper-cased keys.

    Not filtered by ``is_active``: a plan priced in a currency somebody has since
    deactivated must still render with its real symbol. ``is_active`` governs what may be
    OFFERED in a dropdown, not how an existing amount is displayed.

    Never raises -- an unreadable table degrades to the code-and-2-places fallbacks
    below, because a pricing page with no prices is worse than one with plain codes.
    """
    try:
        return {
            (row.currency_code or "").strip().upper(): row
            for row in CurrencyInfo.objects.all()
        }
    except Exception:  # noqa: BLE001 - a currency lookup must not cost the amount
        logger.exception("money: could not read currency_info; using fallbacks")
        return {}


def _row(currencies: dict, currency_code) -> CurrencyInfo | None:
    if not currency_code:
        return None
    return currencies.get(str(currency_code).strip().upper())


def decimal_places(currencies: dict, currency_code) -> int:
    """How many minor units make one major unit."""
    if not currency_code:
        logger.warning("money: no currency code given; assuming 2 decimal places")
        return FALLBACK_DECIMAL_PLACES
    row = _row(currencies, currency_code)
    if row is None or row.decimal_places is None:
        return FALLBACK_DECIMAL_PLACES
    return int(row.decimal_places)


def symbol(currencies: dict, currency_code) -> str:
    """The display symbol, or the upper-cased code, or "" when there is no currency.

    The code is upper-cased before the lookup. One of the two Flask callers this
    replaces did not, so a lowercase code missed the row and rendered the bare code even
    where a symbol existed.
    """
    if not currency_code:
        return ""
    code = str(currency_code).strip().upper()
    row = _row(currencies, code)
    if row is not None and row.symbol:
        return row.symbol
    return code


def to_major(currencies: dict, amount_minor, currency_code) -> Decimal:
    """Minor units to a major-unit Decimal. HKD 28000 -> 280.00; JPY 280 -> 280."""
    if amount_minor is None:
        return Decimal("0")
    places = decimal_places(currencies, currency_code)
    return Decimal(int(amount_minor)) / (Decimal(10) ** places)


def format_minor(currencies: dict, amount_minor, currency_code) -> str:
    """Minor units as a plain grouped decimal -- no symbol, no currency code.

    The currency is stated separately by every caller. Repeating it here invites the two
    disagreeing.
    """
    places = decimal_places(currencies, currency_code)
    value = abs(Decimal(int(amount_minor or 0))) / (Decimal(10) ** places)
    return f"{value:,.{places}f}"
