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
from zoneinfo import available_timezones

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


#: National-number lengths held exactly. Hong Kong only for now: it is the one market Minty
#: sells in (the user, 2026-10-12) - add a code when its market opens, not before. Every other
#: code gets E.164's own bounds: at least 4 national digits, at most 15 digits in all. The
#: wizard's ``PHONE_LENGTHS`` (lib/validation.ts) is the same table -- change them together.
PHONE_LENGTHS = {"852": 8}

#: Dial codes where a leading 0 is part of the number (Italy and the Vatican, San Marino).
#: Everywhere else a leading 0 is the domestic trunk prefix, which E.164 drops.
KEEPS_LEADING_ZERO = {"39", "378"}

#: The wizard words each failure itself; this is the API's one generic answer.
PHONE_INVALID = "Please check the phone number and its country code."

_PHONE_SHAPE = re.compile(r"\+[0-9 ()\-.]+")


#: ``entities.timezone`` is VARCHAR(30); a few IANA names are longer and cannot be stored.
TIMEZONE_MAX = 30
TIMEZONE_INVALID = "That timezone isn't one I recognise."


def timezone(value) -> tuple[str | None, str]:
    """The browser's IANA zone (``Intl.DateTimeFormat().resolvedOptions().timeZone``) ->
    ``(zone or None, error message)``.

    Step 1 sends it with no field on screen, so an empty value is simply "not sent" (None,
    nothing written). Anything sent must be a real IANA name that fits the column - checked
    here, not trusted, because this is a plain JSON endpoint.
    """
    text = (value or "").strip()
    if not text:
        return None, ""
    if len(text) > TIMEZONE_MAX or text not in available_timezones():
        return None, TIMEZONE_INVALID
    return text, ""


def contact_phone(value) -> tuple[str | None, str]:
    """Payload value -> ``(E.164 phone or None, error message)``.

    The wizard sends ``+<dial code><national number>``; spaces, dashes, dots and brackets
    are tolerated and dropped. The dial code must be one ``country_info.phone_code`` holds
    (E.164 codes are prefix-free, so at most one matches), and the length is re-checked
    here rather than trusted: this is a plain JSON endpoint, so the client's check is a
    convenience and not a control. Stored as ``+85291234567``.

    An empty value returns None, and the caller writes NULL -- that is how a user who
    deletes their number actually gets it cleared, rather than storing "".
    """
    text = (value or "").strip()
    if not text:
        return None, ""
    if not _PHONE_SHAPE.fullmatch(text):
        return None, PHONE_INVALID
    digits = "".join(ch for ch in text if ch.isdigit())
    codes = {
        code.strip().lstrip("+")
        for code in CountryInfo.objects.exclude(phone_code__isnull=True)
        .values_list("phone_code", flat=True)
        .distinct()
        if code.strip().lstrip("+")
    }
    dial = next((digits[:n] for n in (3, 2, 1) if digits[:n] in codes), None)
    if dial is None:
        return None, PHONE_INVALID
    national = digits[len(dial):]
    if national.startswith("0") and dial not in KEEPS_LEADING_ZERO:
        national = national[1:]
    expected = PHONE_LENGTHS.get(dial)
    if expected is not None:
        ok = len(national) == expected
    else:
        ok = len(national) >= 4 and len(dial) + len(national) <= 15
    if not ok:
        return None, PHONE_INVALID
    return f"+{dial}{national}", ""


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
