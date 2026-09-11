"""Error shape for the onboarding API.

THE BODY KEY IS ``error``, NOT ``detail``.

billing-backend answers ``{"detail": ...}`` and its frontend reads that. This
service cannot copy it: the onboarding wizard reads ``result.error`` at some
twenty call sites, and ``lib/errorCopy.js`` resolves user-facing copy from
``data.error ?? data.message``. A ``detail`` body reaches the wizard as an
undefined message and surfaces as a blank toast -- the request failed and the
user is told nothing.

So the Flask contract is preserved verbatim: ``{"error": "<sentence>"}`` with the
same status code Flask used. Every status and body this service returns is
something the existing wizard already knows how to render, which is what lets
the frontend cut over one endpoint group at a time with no frontend change.
"""

import logging

from ninja.errors import AuthenticationError
from ninja.errors import ValidationError as SchemaValidationError

logger = logging.getLogger("minty-onboarding")

# Shown when we have nothing specific and useful to say. Keep it cause-neutral:
# it fires for unknown reasons, so it must not assert one. Same sentence as
# billing-backend's HOUSE_FALLBACK and the wizard's own fallback copy.
HOUSE_FALLBACK = "Something went wrong on my end. Mind trying again?"


class OnboardingValidationError(Exception):
    """Bad input from the wizard. Rendered as 400 with the message as-is.

    400 rather than 422: every hand-written validation failure in Flask's
    onboarding routes answers 400, and the wizard branches on status in places.
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

    Carries the status Flask answered with so a 402 from Stripe, or a 403 on
    consent, reaches the wizard as itself rather than flattened into a 500.
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# django-ninja puts the param source in the first segment of ``loc`` and, for a
# body model, the schema argument name in the second. Neither means anything to
# the person reading the message.
_LOC_NOISE = frozenset({
    "body", "query", "path", "form", "header", "cookie", "file",
    "payload", "data", "request",
})


def _field_names(errors):
    """Readable field names from ninja's ``loc`` tuples, in order, deduplicated."""
    names = []
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        parts = [str(p) for p in (err.get("loc") or ()) if not isinstance(p, bool)]
        parts = [p for p in parts if not p.isdigit()]
        # Strip the source/wrapper segments. If that leaves nothing, the loc
        # named no real field, so say nothing rather than naming 'body'.
        meaningful = [p for p in parts if p not in _LOC_NOISE]
        if not meaningful:
            continue
        name = meaningful[-1].replace("_", " ").strip()
        if name and name not in names:
            names.append(name)
    return names


def _join(names):
    if len(names) == 1:
        return names[0]
    return "{} and {}".format(", ".join(names[:-1]), names[-1])


def validation_message(errors):
    """Turn ninja's ``[{type, loc, msg}, ...]`` into a single sentence.

    ninja's own handler puts that list straight into the body, and the wizard
    would render the serialised JSON in a toast. Whatever reaches a user has to
    be a sentence, so build one here rather than unpicking the structure in the
    frontend. Lifted from billing-backend/core/exceptions.py, which solved this
    for the same reason.
    """
    names = _field_names(errors)
    if not names:
        return "Some of those details didn't look right. Mind checking them?"
    joined = _join(names)
    if all(str(e.get("type", "")).startswith("missing")
           for e in errors if isinstance(e, dict)):
        return "I still need {} to continue.".format(joined)
    return "Mind checking {}? That didn't look quite right.".format(joined)


def register_exception_handlers(api):
    @api.exception_handler(AuthenticationError)
    def on_unauthorized(request, exc):
        """401 with ``{"error": "Unauthorized"}`` -- ninja's default says ``detail``.

        This one is easy to miss and it matters. Every handler below fires on an
        exception the endpoint raised, but an auth failure happens BEFORE the endpoint
        is reached: ninja raises AuthenticationError from the security layer and renders
        its own ``{"detail": "Unauthorized"}``. Without this handler the one status the
        wizard sees most often -- an expired 60-minute token mid-wizard -- arrives in a
        shape it cannot read, and the user gets a blank toast instead of being sent to
        log in again.

        Flask answers ``{"error": "Unauthorized"}`` on exactly this path. Matched.
        """
        return api.create_response(request, {"error": "Unauthorized"}, status=401)

    @api.exception_handler(SchemaValidationError)
    def on_schema_validation(request, exc):
        # The structured errors stay in the log; the body carries the sentence.
        logger.warning("Schema validation error: %s", exc.errors)
        return api.create_response(
            request, {"error": validation_message(exc.errors)}, status=400
        )

    @api.exception_handler(OnboardingValidationError)
    def on_validation(request, exc):
        logger.warning("Validation error: %s", str(exc))
        return api.create_response(request, {"error": str(exc)}, status=400)

    @api.exception_handler(AccessDeniedError)
    def on_access_denied(request, exc):
        logger.warning("Access denied: %s", str(exc))
        return api.create_response(request, {"error": str(exc)}, status=403)

    @api.exception_handler(ConflictError)
    def on_conflict(request, exc):
        logger.info("Conflict: %s", str(exc))
        return api.create_response(request, {"error": str(exc)}, status=409)

    @api.exception_handler(NotFoundError)
    def on_not_found(request, exc):
        logger.warning("Not found: %s", str(exc))
        return api.create_response(request, {"error": str(exc)}, status=404)

    @api.exception_handler(UpstreamError)
    def on_upstream(request, exc):
        logger.warning("Upstream error (%s): %s", exc.status, str(exc))
        return api.create_response(request, {"error": str(exc)}, status=exc.status)

    @api.exception_handler(Exception)
    def on_unhandled(request, exc):
        logger.exception("Unhandled error: %s", str(exc))
        return api.create_response(request, {"error": HOUSE_FALLBACK}, status=500)
