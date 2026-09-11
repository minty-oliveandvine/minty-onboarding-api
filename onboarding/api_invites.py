"""Group E -- team invitations. Split like Group D, for a different reason.

PORTED: ``GET /invite`` (list) and ``POST /invite/cancel``. Both are plain database work.

PROXIED: ``POST /invite``. It creates the row AND sends an email whose accept link points at
a Flask route, rendered from a Flask template. Generating that URL here would be a hardcoded
guess at someone else's routing that fails silently -- as a dead link in an email already
delivered. See onboarding/services/invites.py for the full argument.

So one path is split across two services by METHOD: GET here, POST to Flask. That is fine
mechanically, and it is worth noticing that it is the finest-grained split in the whole
extraction -- a single endpoint whose read half is local and whose write half is not.
"""

from ninja import Body, Router, Schema

from core import minty_client
from core.permissions import entity_for_member

from onboarding.services import invites as invites_service


invites_router = Router()


class CancelInviteIn(Schema):
    invitation_id: str = ""


@invites_router.get("/invite")
def get_invites(request, entity_id: str = ""):
    """Pending invitations for the entity.

    Membership is checked first (404/403 on the entity), then USER_VIEW_ALL inside the
    service -- so a member without the permission gets 403 "Access denied" rather than the
    entity-level message. Both are Flask's.
    """
    entity_for_member(request.auth_user_id, entity_id)
    return invites_service.list_pending(request.auth_user_id, entity_id)


@invites_router.post("/invite")
def post_invite(request, payload: dict = Body(default={})):
    """Create and email an invitation. PROXIED -- see the module header.

    Forwarded whole rather than re-validated here. Re-checking the role or the email
    locally would be a second copy of rules Flask applies anyway, and the two copies would
    be free to disagree about who may invite whom.
    """
    return minty_client.proxy(request, "/api/onboarding/invite", json=payload)


@invites_router.post("/invite/cancel")
def post_invite_cancel(request, payload: CancelInviteIn):
    """Cancel a pending invitation.

    Takes only ``invitation_id``: the entity is read off the invitation, because there is no
    entity in this request to check membership against. The permission and role-rank checks
    inside the service are therefore the ONLY gate here -- there is no entity_for_member call
    above them, and that is deliberate rather than an omission.
    """
    return invites_service.cancel(request.auth_user_id, payload.invitation_id)
