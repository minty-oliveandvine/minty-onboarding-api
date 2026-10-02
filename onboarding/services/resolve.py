"""Turning what the wizard sends into what the columns accept.

``entities.country_code`` and ``entities.currency_id`` are FOREIGN KEYS, into
``country_info`` and ``currency_info``. So these resolvers exist to guarantee one property:
they return a value that is already known to be in the registry, or they return "". They
NEVER return the caller's input unvalidated, because that value goes straight into an FK
column and the insert would fail at commit time -- far from the cause.

WHY THEY ACCEPT SO MANY INPUT FORMS

The wizard sends the canonical value (an alpha-2 code, a currency uuid). The name and
alpha-3 fallbacks are for older callers, and the two resolvers are deliberately as tolerant
as each other: they are fed by the SAME pair of Step 1 dropdowns, so a payload form that
resolves for one and not the other produces an entity with a country and no currency.

"" FOR A NON-EMPTY INPUT IS AN ERROR, NOT A SHRUG

Every caller must treat it that way. Flask's comment on the update path records what
happens otherwise: a supplied-but-unresolvable country used to skip the assignment while
the currency (whose resolver accepts names) still saved, so the two silently diverged and
the 200 gave the wizard no way to notice.

ON LIKE ESCAPING: Flask escapes ``%`` and ``_`` by hand before its ``ilike`` calls, because
a user-supplied wildcard would otherwise widen the match to every row. Django's
``__iexact`` and ``__istartswith`` escape those characters themselves, so the hand-rolled
version is not carried over -- the protection is not dropped, it moved into the ORM.
"""

import re
import uuid as _uuid

from shared_models.models import CountryInfo, CurrencyInfo


def country_code(value) -> str:
    """Payload value -> ``country_info.country_code`` (the ISO alpha-2 PK), or "".

    Tries, in order: the alpha-2 PK, the alpha-3 code, an exact name, then a UNIQUE name
    prefix. The prefix fallback is last and is deliberately unique-only: the registry holds
    the authoritative long form ("Hong Kong SAR China") which a caller sending a display
    label ("Hong Kong") would otherwise miss, but an ambiguous prefix resolves to "" rather
    than guessing between two countries.
    """
    text = (value or "").strip()
    if not text:
        return ""

    row = CountryInfo.objects.filter(country_code=text.upper()).first()

    if row is None and len(text) == 3:
        row = CountryInfo.objects.filter(alpha3_code=text.upper()).first()

    if row is None:
        row = CountryInfo.objects.filter(country_name_en__iexact=text).first()

    if row is None:
        # limit 2: enough to tell unique from ambiguous without fetching the rest.
        matches = list(
            CountryInfo.objects.filter(country_name_en__istartswith=text)[:2]
        )
        if len(matches) == 1:
            row = matches[0]

    return row.country_code if row else ""


def currency_id(value) -> str:
    """Payload value -> ``currency_info.id`` as a string uuid, or "".

    The uuid PK is only queried once the value is known to BE a uuid. That guard is not
    tidiness: on Postgres, comparing a uuid column against a malformed literal raises and
    aborts the surrounding transaction, so a typo in one field would take down the whole
    write. Flask carries the same guard with the same note.

    Returns a str, never a ``UUID`` object -- the value is compared against and written to
    columns the rest of this service treats as strings.
    """
    text = (value or "").strip()
    if not text:
        return ""

    row = None
    try:
        _uuid.UUID(text)
    except ValueError:
        pass
    else:
        row = CurrencyInfo.objects.filter(id=text).first()

    if row is None:
        row = CurrencyInfo.objects.filter(currency_code=text.upper()).first()
    if row is None:
        row = CurrencyInfo.objects.filter(currency_name__iexact=text).first()

    return str(row.id) if row else ""


def contact_phone(value) -> tuple[str | None, str]:
    """Payload value -> ``(stored phone or None, error message)``.

    Digits only, mirroring the wizard, which strips punctuation before sending. The length
    is re-checked here rather than trusted: this is a plain JSON endpoint, so the client's
    ``maxLength`` is a convenience and not a control.

    An empty value returns None, and the caller writes NULL -- that is how a user who
    deletes their number actually gets it cleared, rather than storing "".
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return None, ""
    if not 8 <= len(digits) <= 11:
        return None, "Phone number must be 8-11 digits."
    return digits, ""


#: One "@", something either side, a dot in the domain, PRINTABLE ASCII ONLY -- the wizard's
#: rule (minty-web ``lib/emailInput.ts`` ``EMAIL_RE`` and its copies) and minty-subscription-api's.
EMAIL_RE = re.compile(r"[\x21-\x3F\x41-\x7E]+@[\x21-\x3F\x41-\x7E]+\.[\x21-\x3F\x41-\x7E]+")

#: The frontends' ``EMAIL_ASCII_HINT``, word for word.
EMAIL_NOT_ENGLISH = "Email can only contain English letters, numbers and symbols."


def business_email(value) -> tuple[str | None, str]:
    """Payload value -> ``(stored email or None, error message)``.

    DELIBERATELY SHALLOW: ``EMAIL_RE`` and nothing more. This column is the company's public
    contact address -- it never authenticates anybody and nothing is ever sent to it to
    confirm it -- so a stricter parser would reject legitimate addresses for no gain. Empty
    clears the field.

    ENGLISH ONLY (2026-10-01): a non-ASCII character (Korean, accents) is refused in its own
    words, before the shape check -- it is a rule, not a typo. Stored rows are not rewritten.
    """
    email = (value or "").strip()
    if not email:
        return None, ""
    if len(email) > 100:
        return None, "Business email must be 100 characters or fewer."
    if not email.isascii():
        return None, EMAIL_NOT_ENGLISH
    if not EMAIL_RE.fullmatch(email):
        return None, "Please enter a valid business email."
    return email, ""
