"""Team invitations -- listing and cancelling. Sending stays in Flask.

Ported from Minty's ``blueprints/entity/services/onboarding_invites.py``, minus
``send_invite``.

WHY SENDING IS NOT PORTED, AND WHY THAT IS A DIFFERENT ARGUMENT FROM STRIPE AND XERO

Stripe and Xero have a hard single-writer constraint -- a second refresher literally breaks
the customer's connection. Email has no such constraint; two services can both send mail.
So the reason here is different, and worth stating rather than waving at "the write rule":

  * THE LINK POINTS AT A FLASK ROUTE. The accept URL is built with
    ``url_for("invitation.accept_invitation_page")``, and the page it opens, the token
    handling, and the OTP path that creates a User row for a non-Xero invitee all live in
    Flask. A URL generated here would be a hardcoded guess at someone else's route, and it
    would break SILENTLY -- as a dead link in an email already sent, discovered by a
    customer.
  * THE COPY LIVES IN A FLASK TEMPLATE. Invite wording is user-facing text this repo keeps
    in one place on purpose (see ERROR_COPY.md). Two copies drift, and the drift ships to
    customers.
  * It would mean a second service holding Brevo credentials for no gain.

So ``POST /invite`` is proxied. Listing and cancelling are plain database work with no
email, no external system and no Flask-owned URL, so they are ported.

THE ROLE-RANK RULE

Both operations gate on ``USER_INVITE`` (shop_manager and above) AND on rank: you may not
cancel an invitation for a role at or above your own. Without the second check, a
shop_manager could cancel the admin invitation that would have outranked them.
"""

import logging

from core.exceptions import (AccessDeniedError, NotFoundError,
                             OnboardingValidationError)
from core.policy import (Permission, can_manage_role_assignment_for_entity,
                         has_permission_by_user_id)
from shared_models.models import Invitation, User

logger = logging.getLogger("minty-onboarding")

#: Roles the onboarding invite step may assign. Mirrors the Settings dropdown, which
#: excludes entity_base (no standing at all) and super_admin (granted, never invited).
ASSIGNABLE_ROLES = frozenset({"cashier", "shop_manager", "accountant", "admin"})


def normalize_role(name) -> str:
    if not name:
        return ""
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


def invite_payload(inv: Invitation) -> dict:
    """The shape the wizard's pending-invite cards render.

    ``first_name`` / ``last_name`` are persisted on the row rather than living only in the
    accept URL -- before that, a name was lost as soon as the invite was re-read from the
    database, and a resumed wizard showed a card with an email and no name.
    """
    return {
        "id": inv.id,
        "email": inv.email,
        "role": inv.role,
        "status": inv.status,
        "first_name": inv.first_name or "",
        "last_name": inv.last_name or "",
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
    }


def _pending(entity_id: str):
    return Invitation.objects.filter(entity_id=entity_id, status="pending").order_by(
        "-created_at"
    )


def list_pending(user_id, entity_id: str) -> dict:
    """The entity's pending invitations. Raises 403 without USER_VIEW_ALL.

    For the endpoint. ``/state`` uses :func:`list_pending_best_effort` instead -- see there.
    """
    if not has_permission_by_user_id(user_id, Permission.USER_VIEW_ALL, entity_id):
        raise AccessDeniedError("Access denied")
    return {"invitations": [invite_payload(inv) for inv in _pending(entity_id)]}


def list_pending_best_effort(user_id, entity_id: str) -> list[dict]:
    """Pending invitations, or an empty list if the caller may not see them.

    For ``/state``, where a permission gap must NOT fail the whole read: a cashier invited
    into a half-finished onboarding legitimately lacks USER_VIEW_ALL, and their resume has
    to work. What is being protected is the resume, not the invite list.
    """
    if not has_permission_by_user_id(user_id, Permission.USER_VIEW_ALL, entity_id):
        return []
    return [invite_payload(inv) for inv in _pending(entity_id)]


def cancel(user_id, invitation_id: str) -> dict:
    """Cancel a pending invitation. Soft: the row is marked ``cancelled``, never deleted.

    Note the two different "not found" answers, both ported:

      * no row with this id at all -> 404 "Invitation not found."
      * a row that is no longer PENDING -> 400 "Invitation not found or already processed."

    The second is not a worse-worded 404. It means the invitation existed and something
    already happened to it -- accepted, expired, or cancelled by someone else -- and the
    wizard should refresh rather than treat the id as bogus.
    """
    invitation_id = (invitation_id or "").strip()
    if not invitation_id:
        raise OnboardingValidationError("invitation_id is required")

    invitation = Invitation.objects.filter(id=invitation_id).first()
    if invitation is None:
        raise NotFoundError("Invitation not found.")

    # The entity comes from the INVITATION, not from the caller. There is no entity_id in
    # this request to check membership against, so the invitation's own entity is the scope.
    if not has_permission_by_user_id(
        user_id, Permission.USER_INVITE, invitation.entity_id
    ):
        raise AccessDeniedError("Not authorized.")

    user = User.objects.filter(id=str(user_id)).first()
    if not can_manage_role_assignment_for_entity(
        user, invitation.role, invitation.entity_id
    ):
        raise AccessDeniedError(
            "You cannot cancel an invitation for a role equal to or higher than your own."
        )

    updated = Invitation.objects.filter(id=invitation_id, status="pending").update(
        status="cancelled"
    )
    if not updated:
        # Lost a race, or it was already resolved. Checked as part of the UPDATE rather
        # than re-read first, so two concurrent cancels cannot both report success.
        raise OnboardingValidationError("Invitation not found or already processed.")

    logger.info("onboarding: invitation %s cancelled by %s", invitation_id, user_id)
    return {"status": "success"}
