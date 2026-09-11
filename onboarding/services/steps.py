"""The wizard's step table. THE single source of truth.

Before this, the ordering existed twice: as a ``STEPS`` array in the frontend's
``OnboardingApp.jsx`` and as ``STEP_*`` constants in Flask's ``onboarding_state.py``, with
the frontend's ``deriveResumeStep`` recomputing what the backend had already derived. Two
copies of an ordering is one copy too many -- and they had already diverged in meaning,
which is the subtle part:

    ``saved_step`` is the FRONTEND step id, stored verbatim.
    ``current_step`` is the BACKEND's derived landing step.

They are numbered on the same scale and they do not mean the same thing. ``saved_step`` is
where the user pressed "Save and Exit"; ``current_step`` is the furthest step the saved
DATA justifies. Conflating them lands somebody on a step whose prerequisites are not met.

This module owns the numbering, and ``/api/onboarding/state`` returns the table so the
frontend can render labels from it and delete its own copy.
"""

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

#: id -> label, in wizard order. Labels match what the frontend renders today, so it can
#: adopt this table without a visible change.
STEPS: tuple[tuple[int, str], ...] = (
    (STEP_BASIC, "Basic Information"),
    (STEP_MODULE, "Select Module"),
    (STEP_INVITE, "User Invite"),
    (STEP_ACCOUNTING, "Connect to Accounting System"),
    (STEP_SALES, "Sales Setting"),
    (STEP_ACCOUNT_CODE, "Account Code Setting"),
    (STEP_OTHERS, "Others"),
    (STEP_BILLS, "Payment Settings"),
    (STEP_ALL_SET, "All Set"),
)


def step_table() -> list[dict]:
    """The table as JSON, for the ``steps`` key of the state response."""
    return [{"id": sid, "label": label} for sid, label in STEPS]


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
