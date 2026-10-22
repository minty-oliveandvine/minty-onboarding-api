"""This service's refusals, rendered in the one envelope every Minty API answers with
(``minty_shared.django.errors``): ``{"error": "<sentence>"}``, never ``detail`` - the wizard reads
``result.error`` at some twenty call sites, and ``@minty/shared``'s client reads ``error`` first.

The wizard's own refusals keep Flask's statuses, because the wizard branches on them: bad input
400, not a member 403, no such company 404, a clash 409, and a proxied call's own status (a 402
declined card, a 403 missing consent) as itself. Input the schema refuses is 422 with ``fields``.
"""

import logging

from minty_shared.django.errors import HOUSE_FALLBACK, register_error_handlers

__all__ = [
    "HOUSE_FALLBACK",
    "AccessDeniedError",
    "ConflictError",
    "NotFoundError",
    "OnboardingValidationError",
    "UpstreamError",
    "register_exception_handlers",
]

logger = logging.getLogger("minty-onboarding")


class OnboardingValidationError(Exception):
    """Bad input from the wizard. Rendered as 400 with the message as-is.

    400 rather than 422: every hand-written validation failure in Flask's onboarding routes
    answers 400, and the wizard branches on status in places.
    """


class AccessDeniedError(Exception):
    """Caller is authenticated but not a member of the entity they named. 403."""


class NotFoundError(Exception):
    """The named entity does not exist. 404."""


class ConflictError(Exception):
    """The write clashes with something that already exists. 409.

    Flask decides this by sniffing the message (``409 if "exist" in error.lower()``), which
    couples the status to the wording of a user-facing sentence -- rephrase the copy and the
    status silently changes. A distinct exception type instead.
    """


class UpstreamError(Exception):
    """A proxied call to Flask failed.

    Carries the status Flask answered with so a 402 from Stripe, or a 403 on consent, reaches
    the wizard as itself rather than flattened into a 500.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def register_exception_handlers(api):
    register_error_handlers(
        api,
        logger=logger,
        refusals={
            OnboardingValidationError: 400,
            AccessDeniedError: 403,
            NotFoundError: 404,
            ConflictError: 409,
            UpstreamError: lambda exc: exc.status,
        },
    )
