"""Who may touch which company.

One rule, and it is the same one every ``/api/onboarding/*`` route in Flask applies:
the caller must hold a ``user_entity`` row for the entity they named. There is no role
matrix here -- unlike billing-backend, which grades cashier / shop_manager / accountant
/ admin against each action. Onboarding runs before any of that exists: the person
walking the wizard created the company thirty seconds ago and is its only member.

THE STATUS CODES ARE PART OF THE CONTRACT.

Ported from ``_entity_for_member`` (Minty blueprints/entity/routes/create.py:721) and
kept byte-compatible with it, because the wizard already branches on these:

    no entity_id given  -> 400  "entity_id is required"
    caller not a member -> 403  "You don't have access to this entity"
    entity does not exist -> 404  "Entity not found"

Note the order: membership is checked BEFORE existence. That is deliberate and worth not
"fixing" -- checking existence first would let anyone probe which entity ids are real by
reading 403 against 404.
"""

from core.exceptions import AccessDeniedError, NotFoundError, OnboardingValidationError
from shared_models.models import Entity, UserEntity


def require_entity_id(entity_id) -> str:
    """The trimmed entity id, or raise the 400 Flask raises."""
    entity_id = (entity_id or "").strip()
    if not entity_id:
        raise OnboardingValidationError("entity_id is required")
    return entity_id


def is_member(user_id, entity_id: str) -> bool:
    """Does this user hold a user_entity row for this entity?"""
    return UserEntity.objects.filter(
        user_id=str(user_id), entity_id=entity_id
    ).exists()


def entity_for_member(user_id, entity_id) -> Entity:
    """The entity, if ``user_id`` is a member of it. Raises otherwise.

    The single gate in front of every endpoint that names an entity. Raising rather
    than returning ``(entity, error)`` as Flask does: the exception handlers in
    core/exceptions.py render each of these to the same status and body Flask sent, so
    the wire contract is identical while the call sites stay one line.
    """
    entity_id = require_entity_id(entity_id)

    if not is_member(user_id, entity_id):
        raise AccessDeniedError("You don't have access to this entity")

    entity = Entity.objects.filter(id=entity_id).first()
    if entity is None:
        raise NotFoundError("Entity not found")
    return entity
