"""Archived companies (``entities.archived_at``, minty-db v0.5; owner, 2026-11-10).

An archived company is refused at the one gate exactly as a company the person doesn't belong to
is - same status, same sentence, superusers included - so nothing says it exists. And an archived
onboarding company is never the one the wizard resumes.
"""

import uuid
from datetime import UTC, datetime

import pytest
from minty_db.models import Entity, User, UserEntity

from core.exceptions import AccessDeniedError
from core.permissions import entity_for_member
from onboarding.services.entity_create import find_resumable_entity
from tests.conftest import make_token

STATE = "/api/onboarding/sales-methods"  # gated by entity_for_member, no Postgres view
REFUSED = "You don't have access to this entity"


def _archive(entity):
    Entity.objects.filter(id=entity.id).update(archived_at=datetime.now(UTC))


@pytest.mark.django_db
def test_a_member_is_refused_an_archived_company_as_if_not_a_member(client, auth, entity, user):
    _archive(entity)
    resp = client.get(STATE, {"entity_id": str(entity.id)}, **auth)
    assert resp.status_code == 403
    assert resp.json() == {"error": REFUSED}
    with pytest.raises(AccessDeniedError, match=REFUSED):
        entity_for_member(user.id, str(entity.id))


@pytest.mark.django_db
def test_a_superuser_member_is_refused_too(client, entity):
    boss = User.objects.create(
        id=str(uuid.uuid4()),
        email="boss@example.com",
        username="boss@example.com",
        password="x",
        system_role="superadmin",
        approved=True,
    )
    UserEntity.objects.create(
        user_id=boss.id, entity_id=entity.id, role="super_admin", approved=True
    )
    _archive(entity)
    resp = client.get(
        STATE, {"entity_id": str(entity.id)}, HTTP_AUTHORIZATION=f"Bearer {make_token(boss.id)}"
    )
    assert resp.status_code == 403 and resp.json() == {"error": REFUSED}


@pytest.mark.django_db
def test_an_active_company_still_opens(client, auth, entity):
    assert entity_for_member  # the gate is unchanged for a company that is not archived
    Entity.objects.filter(id=entity.id).update(archived_at=None)
    assert client.get(STATE, {"entity_id": str(entity.id)}, **auth).status_code == 200


@pytest.mark.django_db
def test_an_archived_onboarding_company_is_never_resumed(user, entity):
    assert str(find_resumable_entity(user.id, entity.name).id) == str(entity.id)
    _archive(entity)
    assert find_resumable_entity(user.id, entity.name) is None
