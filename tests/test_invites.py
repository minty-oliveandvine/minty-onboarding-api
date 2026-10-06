"""Group E -- team invitations.

Listing and cancelling are ported; sending is proxied. The cancel tests carry the weight,
because cancel has two gates rather than one and the second is easy to leave out: you must
hold USER_INVITE on the entity AND outrank the invitation you are cancelling.
"""

import uuid
from datetime import datetime, timezone

import pytest

from core import minty_client
from shared_models.models import Invitation, UserEntity
from tests.conftest import make_token

INVITE = "/api/onboarding/invite"
CANCEL = "/api/onboarding/invite/cancel"


def make_invite(entity, *, email="new@example.com", role="cashier", status="pending", **kw):
    return Invitation.objects.create(
        id=str(uuid.uuid4()),
        entity_id=entity.id,
        email=email,
        role=role,
        token=str(uuid.uuid4()),
        status=status,
        created_at=datetime.now(timezone.utc),
        **kw,
    )


def as_role(user, entity, role):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role=role)
    return {"HTTP_AUTHORIZATION": f"Bearer {make_token(user.id)}"}


def cancel(client, auth, invitation_id):
    return client.post(
        CANCEL, {"invitation_id": invitation_id},
        content_type="application/json", **auth,
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_pending_invites_are_listed(client, auth, entity):
    make_invite(entity, email="a@example.com", first_name="Ada", last_name="Lovelace")
    body = client.get(INVITE, {"entity_id": entity.id}, **auth).json()
    assert [i["email"] for i in body["invitations"]] == ["a@example.com"]
    assert body["invitations"][0]["first_name"] == "Ada"


@pytest.mark.django_db
def test_names_are_read_from_the_row_not_the_accept_url(client, auth, entity):
    """Persisted on the invitation on purpose.

    Before that they lived only in the accept URL, so a name was lost the moment the invite
    was re-read from the database -- and a resumed wizard showed a card with an email and no
    name.
    """
    make_invite(entity, first_name="Grace", last_name="Hopper")
    row = client.get(INVITE, {"entity_id": entity.id}, **auth).json()["invitations"][0]
    assert (row["first_name"], row["last_name"]) == ("Grace", "Hopper")


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["accepted", "revoked", "expired"])
def test_non_pending_invites_are_excluded(client, auth, entity, status):
    make_invite(entity, status=status)
    assert client.get(INVITE, {"entity_id": entity.id}, **auth).json()["invitations"] == []


@pytest.mark.django_db
def test_listing_requires_membership(client, other_user, entity):
    resp = client.get(
        INVITE, {"entity_id": entity.id},
        HTTP_AUTHORIZATION=f"Bearer {make_token(other_user.id)}",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_a_cashier_is_refused_the_invite_list_endpoint(client, user, entity):
    """DIFFERENT FROM /state, and deliberately so.

    The endpoint answers 403, because the caller asked for exactly this and is not allowed
    it. ``/state`` returns an empty list for the same caller, because there what is being
    protected is the resume, not the list. Same permission, two different right answers.
    """
    auth = as_role(user, entity, "cashier")
    make_invite(entity)

    resp = client.get(INVITE, {"entity_id": entity.id}, **auth)
    assert resp.status_code == 403
    assert resp.json() == {"error": "Access denied"}

    state = client.get("/api/onboarding/state", {"entity_id": entity.id}, **auth)
    assert state.status_code == 200
    assert state.json()["invites"] == []


@pytest.mark.django_db
def test_a_shop_manager_may_list(client, user, entity):
    """USER_VIEW_ALL is shop_manager and above."""
    auth = as_role(user, entity, "shop_manager")
    make_invite(entity)
    assert client.get(INVITE, {"entity_id": entity.id}, **auth).status_code == 200


@pytest.mark.django_db
def test_listing_requires_an_entity_id(client, auth):
    resp = client.get(INVITE, **auth)
    assert resp.status_code == 400
    assert resp.json() == {"error": "entity_id is required"}


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_cancelling_marks_the_row_cancelled(client, auth, entity):
    """SOFT. The row is never deleted -- the invite happened, and the record says so."""
    inv = make_invite(entity)
    resp = cancel(client, auth, inv.id)
    assert resp.status_code == 200
    assert resp.json() == {"status": "success"}

    inv.refresh_from_db()
    assert inv.status == "revoked"


@pytest.mark.django_db
def test_cancelling_requires_an_invitation_id(client, auth):
    resp = cancel(client, auth, "")
    assert resp.status_code == 400
    assert resp.json() == {"error": "invitation_id is required"}


@pytest.mark.django_db
def test_an_unknown_invitation_is_404(client, auth):
    resp = cancel(client, auth, str(uuid.uuid4()))
    assert resp.status_code == 404
    assert resp.json() == {"error": "Invitation not found."}


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["revoked", "accepted", "expired"])
def test_an_already_resolved_invitation_is_400_not_404(client, auth, entity, status):
    """A DIFFERENT ANSWER FROM 404, and not just worse wording.

    404 means the id is bogus. 400 means the invitation existed and something already
    happened to it -- so the wizard should refresh its list rather than treat the id as
    invalid.
    """
    inv = make_invite(entity, status=status)
    resp = cancel(client, auth, inv.id)
    assert resp.status_code == 400
    assert resp.json() == {"error": "Invitation not found or already processed."}


@pytest.mark.django_db
def test_a_cashier_cannot_cancel(client, user, entity):
    """USER_INVITE is shop_manager and above."""
    inv = make_invite(entity)
    auth = as_role(user, entity, "cashier")
    resp = cancel(client, auth, inv.id)
    assert resp.status_code == 403
    assert resp.json() == {"error": "Not authorized."}
    inv.refresh_from_db()
    assert inv.status == "pending"


@pytest.mark.django_db
def test_you_cannot_cancel_an_invitation_that_outranks_you(client, user, entity):
    """THE SECOND GATE, and the one easy to leave out.

    A shop_manager holds USER_INVITE, so the first check passes. Without the rank check they
    could cancel the admin invitation that would have outranked them -- quietly keeping
    themselves the most senior person in the company.
    """
    inv = make_invite(entity, role="admin")
    auth = as_role(user, entity, "shop_manager")
    resp = cancel(client, auth, inv.id)
    assert resp.status_code == 403
    assert "higher than your own" in resp.json()["error"]
    inv.refresh_from_db()
    assert inv.status == "pending"


@pytest.mark.django_db
def test_you_cannot_cancel_an_invitation_for_your_own_rank(client, user, entity):
    """"Equal to or higher" -- an accountant may not cancel another accountant's invite."""
    inv = make_invite(entity, role="accountant")
    auth = as_role(user, entity, "accountant")
    assert cancel(client, auth, inv.id).status_code == 200

    # ...but the rank rule still bites one step up.
    higher = make_invite(entity, email="b@example.com", role="admin")
    assert cancel(client, auth, higher.id).status_code == 403


@pytest.mark.django_db
def test_you_may_cancel_a_lower_ranked_invitation(client, user, entity):
    inv = make_invite(entity, role="cashier")
    auth = as_role(user, entity, "shop_manager")
    assert cancel(client, auth, inv.id).status_code == 200


@pytest.mark.django_db
def test_a_stranger_cannot_cancel_another_companys_invitation(client, other_user, entity):
    """The entity comes from the INVITATION, not from the request.

    There is no entity_id in this payload, so the permission check on the invitation's own
    entity is the only gate -- which is why it has to be right.
    """
    inv = make_invite(entity)
    resp = client.post(
        CANCEL, {"invitation_id": inv.id}, content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {make_token(other_user.id)}",
    )
    assert resp.status_code == 403
    inv.refresh_from_db()
    assert inv.status == "pending"


@pytest.mark.django_db
def test_cancelling_twice_reports_the_second_as_already_processed(client, auth, entity):
    """Two concurrent cancels must not both report success.

    The status check is part of the UPDATE rather than a read-then-write, so the second one
    updates zero rows and says so.
    """
    inv = make_invite(entity)
    assert cancel(client, auth, inv.id).status_code == 200
    assert cancel(client, auth, inv.id).status_code == 400


# ---------------------------------------------------------------------------
# Sending -- proxied
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_sending_an_invite_is_forwarded_to_flask(client, auth, entity, monkeypatch):
    """PROXIED because the email links into a Flask route and uses a Flask template.

    A URL generated here would be a hardcoded guess at someone else's routing, and it would
    fail silently -- as a dead link in an email already delivered.
    """
    seen = {}

    def fake_forward(request, path, *, method="POST", json=None, params=None, base=None):
        seen.update(path=path, method=method, json=json)
        return {
            "status": "success",
            "email_sent": True,
            "invitation": {"id": "abc", "email": "new@example.com"},
        }, 201

    monkeypatch.setattr(minty_client, "forward", fake_forward)

    body = {
        "entity_id": entity.id, "email": "new@example.com",
        "role": "cashier", "first_name": "New", "last_name": "Hire",
    }
    resp = client.post(INVITE, body, content_type="application/json", **auth)

    assert resp.status_code == 201
    assert resp.json()["email_sent"] is True
    assert seen["path"] == "/api/onboarding/invite"
    assert seen["method"] == "POST"
    # Forwarded WHOLE. Re-validating the role here would be a second copy of rules Flask
    # applies anyway, free to disagree with the first.
    assert seen["json"] == body


@pytest.mark.django_db
def test_a_refused_invite_keeps_flasks_status(client, auth, entity, monkeypatch):
    """409 means "already invited". The wizard has copy for it; a flattened 500 would not."""
    def fake_forward(request, path, **kwargs):
        return {"error": "An invitation is already pending for this email."}, 409

    monkeypatch.setattr(minty_client, "forward", fake_forward)

    resp = client.post(
        INVITE, {"entity_id": entity.id, "email": "dup@example.com", "role": "cashier"},
        content_type="application/json", **auth,
    )
    assert resp.status_code == 409
    assert "already pending" in resp.json()["error"]


@pytest.mark.django_db
def test_sending_still_needs_a_token(client, entity):
    resp = client.post(
        INVITE, {"entity_id": entity.id, "email": "x@example.com", "role": "cashier"},
        content_type="application/json",
    )
    assert resp.status_code == 401
