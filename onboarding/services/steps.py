"""The wizard's step numbering, as the backend understands it.

IMPORTANT: THIS IS NOT THE FRONTEND'S ORDERING, AND MUST NOT BE MADE INTO IT.

An earlier version of this module claimed to be "THE single source of truth" and published a
label table for the frontend to adopt. That was wrong, and the mistake is worth recording so
it is not repeated.

The frontend derives its landing step from a DIFFERENT ordering on purpose.
``OnboardingApp.jsx`` documents that this module's ``current_step`` "is derived from a
different ordering (modules -> Xero -> petty-cash -> bills/invite) than the FE flow", refuses
to use it as a landing step, and records the bug that trusting it caused: "resume jumped
straight to Connect to Accounting". It uses ``current_step`` / ``max_reached`` only to raise
the ceiling on steps already unlocked. Its own table also carries short and tiny label
variants and module-conditional grouping that this one never had.

So the two are different questions with different answers, not two copies of one. The label
table and its ``step_table()`` serialiser were withdrawn once that was established; what
remains is the numbering these two things genuinely need:

    ``derive_current_step``  (services/state.py) -- which step the saved DATA justifies
    ``is_valid_step``        -- the 1..9 bound on a ``saved_step`` the wizard sends

``saved_step`` is the FRONTEND step id, stored verbatim. ``current_step`` is this backend's
derivation. They share a scale and mean different things; conflating them lands somebody on a
step whose prerequisites are not met.
"""

# Every step the wizard has, kept complete even where this module does not reference one:
# the numbering is a fact about the product, and a gap at 6 and 7 would read as though those
# steps did not exist.
STEP_BASIC = 1
STEP_MODULE = 2
STEP_INVITE = 3
STEP_ACCOUNTING = 4
STEP_SALES = 5
STEP_ACCOUNT_CODE = 6
STEP_OTHERS = 7
STEP_BILLS = 8
STEP_ALL_SET = 9

FIRST_STEP = STEP_BASIC
LAST_STEP = STEP_ALL_SET


def is_valid_step(value) -> bool:
    """Whether ``value`` is a step id the wizard could legitimately have saved.

    STRICTER THAN FLASK, ON PURPOSE -- a narrow, deliberate divergence.

    Flask validates with a bare ``int(saved_step)``, so ``2.5`` does not raise: it
    truncates to ``2`` and stores it. The user then resumes on a step they were never on,
    and nothing reports a problem. ``True`` is worse in the same way, because ``bool`` is
    an ``int`` subclass and ``int(True)`` is ``1``.

    This is a WRITE, and the value written is the one the wizard navigates by, so a
    silently different number is worse than a refusal. The wizard only ever sends integers,
    so refusing the rest changes nothing in practice -- which is exactly why it is cheap to
    be strict here.

    Digit strings stay accepted: Flask takes them, and a JSON client sending "5" is being
    unremarkable rather than wrong.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        step = value
    elif isinstance(value, str):
        text = value.strip()
        if not (text.lstrip("+-").isdigit() and text.lstrip("+-")):
            return False
        step = int(text)
    else:
        # Floats included. An integral float is still not an integer step id.
        return False
    return FIRST_STEP <= step <= LAST_STEP
