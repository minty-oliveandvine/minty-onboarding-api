"""Group B -- wizard state. Two endpoints, and the only column this service owns.

``GET /state`` is the most important endpoint in the service. It is what makes a cold
resume work: a new browser with no localStorage gets the whole wizard picture back from the
database, and it reads across every domain the wizard touches -- company, modules, Xero,
petty-cash config, invites.

``POST /saved-step`` writes ``entities.onboarding_saved_step``. That is the one column
onboarding genuinely owns.

THE TWO STEP NUMBERS ARE NOT THE SAME NUMBER

    saved_step    the FRONTEND step id, stored and returned verbatim
    current_step  the BACKEND's derived landing step

Both are 1-9 and they mean different things -- where the user pressed "Save and Exit"
versus how far the saved data justifies going. The wizard resumes at ``saved_step`` but is
held back by ``current_step``'s prerequisites, which is why ``/saved-step`` does no
remapping on the way in or out.
"""

import logging

from ninja import Router, Schema
from pydantic import Field

from core.exceptions import OnboardingValidationError
from core.permissions import entity_for_member

from onboarding.services import state as state_service
from onboarding.services import steps as step_defs

logger = logging.getLogger("minty-onboarding")

state_router = Router()


class SavedStepIn(Schema):
    """``entity_id`` is in the BODY, not a header -- see core/auth.py."""

    entity_id: str = ""
    # Deliberately permissive at the schema level so a bad value produces Flask's exact
    # 400 sentence from the validator below, rather than ninja's field-name message.
    # The wizard has copy for "saved_step must be an integer 1-9" and none for ninja's.
    saved_step: object = Field(default=None)


@state_router.get("/state")
def get_state(request, entity_id: str = ""):
    """The full resume picture for ``entity_id``.

    403 unless the caller is a member of the entity, 404 if it does not exist, 400 if no
    entity_id was given -- checked in that order, so 403-vs-404 cannot be used to probe
    which entity ids are real.

    DIVERGES FROM FLASK BY ONE KEY: the response carries ``steps``, the wizard's step
    table, so the frontend can delete its duplicate copy of the ordering. Additive -- every
    key Flask sends is still sent, unchanged.
    """
    entity = entity_for_member(request.auth_user_id, entity_id)
    return state_service.get_onboarding_state(request.auth_user_id, entity)


@state_router.post("/saved-step")
def post_saved_step(request, payload: SavedStepIn):
    """Persist the wizard's "Save and Exit" step on the entity.

    Stored VERBATIM as the frontend step id (1-9). No remap onto the backend's derived
    ordering -- see the module header for why those are different numbers.
    """
    entity = entity_for_member(request.auth_user_id, payload.entity_id)

    if not step_defs.is_valid_step(payload.saved_step):
        # Flask's exact sentence. The wizard renders it as-is.
        raise OnboardingValidationError("saved_step must be an integer 1-9")
    step = int(payload.saved_step)

    entity.onboarding_saved_step = step
    entity.save(update_fields=["onboarding_saved_step"])
    logger.info("onboarding: entity %s saved at step %s", entity.id, step)
    return {"ok": True, "saved_step": step}
