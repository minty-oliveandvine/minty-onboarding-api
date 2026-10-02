"""Bearer auth for the onboarding API.

THIS SERVICE VERIFIES TOKENS. IT NEVER MINTS THEM.

Flask mints the onboarding JWT (blueprints/entity/routes/create.py::_mint_onboarding_token)
and hands it to the wizard in the launch URL. Flask also owns the email-OTP login and the
``itsdangerous`` session handoff behind it. Everything here is verification, and adding a
mint would create a second issuer for one identity.

HOW THIS DIFFERS FROM minty-payment-request-api's BearerAuth, and why

1. A ROLE ON AN ENTITY IS NOT REQUIRED AT THE DOOR.
   minty-payment-request-api 401s a caller holding no role on the entity in the X-Entity-Id
   header, which is right for it: a bill belongs to a company, so a caller with no
   standing there has no business reaching one. Onboarding cannot do that. Its FIRST
   authenticated call, POST /api/onboarding/create, exists precisely to create the
   company the caller will then have a role on -- there is no entity yet to hold a role
   on. So authentication here answers only "who is this person", and every endpoint that
   names an entity checks membership itself through core.permissions.entity_for_member.

2. THE ENTITY COMES FROM THE QUERY OR BODY, NOT A HEADER.
   Flask reads ``entity_id`` from ``request.args`` or the JSON body, and the wizard
   sends it that way. No X-Entity-Id header is read here, and settings.py deliberately
   leaves it out of CORS_ALLOW_HEADERS, so a header nothing sends cannot be silently
   ignored.

3. THE ``scope`` CLAIM IS CHECKED.
   Flask mints these tokens with ``scope: "onboarding"`` but
   blueprints/shared/bearer_api.py::user_id_from_bearer never verifies it, so today ANY
   HS256 token signed with the shared SECRET_KEY opens this surface -- including the
   longer-lived tokens minted for the Module 2 frontend. Requiring the claim here closes
   that. It is a genuine tightening rather than a port: a token that works against Flask
   today can be refused here.

   ACCEPT_ANY_SCOPE exists to turn it off if a caller nobody predicted breaks. Prefer
   fixing the caller.

WHAT IS DELIBERATELY NOT CARRIED OVER: minty-payment-request-api logs the SECRET_KEY length and a
truncated hash of it on every authentication attempt. That is a debugging aid pointed at
a secret; it is not repeated here.
"""

import logging

import jwt
from django.conf import settings
from ninja.security import HttpBearer

from shared_models.models import User

logger = logging.getLogger("minty-onboarding")

#: Seconds of clock disagreement tolerated on `iat` / `exp` between the minting host
#: and this one. See the note at the decode call.
CLOCK_SKEW_LEEWAY_SECONDS = 60

#: The scope claim Flask puts on an onboarding token.
ONBOARDING_SCOPE = "onboarding"

#: Escape hatch for requirement 3 above. Leave False.
ACCEPT_ANY_SCOPE = False


class OnboardingBearerAuth(HttpBearer):
    """Resolve the caller from the JWT. Attaches ``request.auth_user``.

    Returns the ``User`` on success and ``None`` on every failure -- a missing header, a
    malformed token, a bad signature, an expired one, a wrong scope and an unknown user
    are all simply "not authenticated" to the caller, which gets one 401. Distinguishing
    them in the response would tell an attacker which part of a forged token to fix.
    Flask's decoder takes the same posture, for the same reason.
    """

    def authenticate(self, request, token):
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"],
                # Clock skew between the host that MINTS (Flask) and the host that
                # VERIFIES (this service). PyJWT's default is zero: a token whose `iat`
                # is one second ahead of this machine's clock is refused as "not yet
                # valid", and the wizard reads that as an expired session. Measured on
                # 2026-09-14: a developer laptop ran 6.5s ahead of Render. Sixty seconds
                # is the conventional allowance and does not meaningfully extend the
                # 60-minute lifetime.
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            )
        except jwt.ExpiredSignatureError:
            # Routine, not suspicious: these tokens live 60 minutes and the wizard is a
            # nine-step form somebody walks away from. info, not warning.
            logger.info("Auth rejected: token expired")
            return None
        except (jwt.DecodeError, jwt.InvalidTokenError) as exc:
            logger.warning("Auth rejected: invalid token -- %s", exc)
            return None

        scope = (payload.get("scope") or "").strip().lower()
        if not ACCEPT_ANY_SCOPE and scope != ONBOARDING_SCOPE:
            logger.warning(
                "Auth rejected: token scope %r is not %r",
                scope or "<missing>",
                ONBOARDING_SCOPE,
            )
            return None

        # str(): the ids this is compared against are String(36)/CharField columns, and
        # a decoder that sometimes answered with an int was a real bug on the Flask side
        # (see bearer_api.user_id_from_bearer, which settles on the coercing form).
        user_id = payload.get("user_id")
        if not user_id:
            logger.warning("Auth rejected: token carries no user_id")
            return None
        user_id = str(user_id)

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            logger.warning("Auth rejected: user %s not found", user_id)
            return None

        request.auth_user = user
        request.auth_user_id = user_id
        return user
