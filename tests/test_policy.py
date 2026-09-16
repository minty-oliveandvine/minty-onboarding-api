"""core/policy -- the permission rules, at the branches nothing else reaches.

The endpoint tests exercise the ordinary member paths. What they never reach is the
SUPERUSER half of the table (a system superuser holds no ``user_entity`` row on a
customer's entity and gets read-only access anyway), ``require_approved``, and the
fail-closed cases. Those are here, against the functions directly.

THE DOOR AND THE ROOM ARE GATED DIFFERENTLY, ON PURPOSE

``core.permissions.is_member`` -- the gate in front of every endpoint that names an entity
-- does NOT check ``approved``. ``has_entity_access`` does. That is not drift: Flask's
``_entity_for_member`` (blueprints/entity/routes/create.py) checks membership alone, and
the door here mirrors it for parity. Approval is enforced per PERMISSION, so an
unapproved member can read ``/state`` (their resume has to work) but is refused anything
that needs a role. The last tests pin both halves.
"""

import uuid
from types import SimpleNamespace

import pytest

from core import permissions, policy
from core.policy import Permission, Role
from shared_models.models import User, UserEntity


def person(**over):
    """A user-shaped object; ``policy`` reads attributes, never the ORM."""
    base = {"id": str(uuid.uuid4()), "system_role": "normal", "role": None}
    base.update(over)
    return SimpleNamespace(**base)


# --- is_superuser -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "system_role", ["superadmin", "SUPERADMIN", " Superadmin ", "superuser", "SUPERUSER"]
)
def test_superuser_by_system_role_in_any_casing(system_role):
    # ``superadmin`` is the database's word (system_role enum, C1); ``superuser`` is the
    # pre-rename spelling that older JWTs still carry and is normalised to it.
    assert policy.is_superuser(person(system_role=system_role)) is True


@pytest.mark.parametrize("system_role", ["normal", "", "admin", "super_admin"])
def test_other_system_roles_are_not_superuser(system_role):
    # `super_admin` is an ENTITY role, not the system one -- the two are separate axes.
    assert policy.is_superuser(person(system_role=system_role)) is False


@pytest.mark.parametrize("role", ["admin", "super_admin", "Super Admin", "super-admin"])
def test_legacy_rows_fall_back_to_the_role_column(role):
    # `system_role` was split out of `role`; rows that predate the split have only `role`,
    # and on THOSE rows "admin" / "super_admin" meant system superuser.
    assert policy.is_superuser(person(system_role=None, role=role)) is True


def test_a_legacy_role_of_superuser_is_NOT_recognised():
    # Surprising but deliberate: the pre-split column never held the value "superuser",
    # so it is not in LEGACY_SUPERUSER_ROLES. Only the new column spells it that way.
    assert policy.is_superuser(person(system_role=None, role="superuser")) is False
    assert policy.is_superuser(person(system_role=None, role="cashier")) is False


def test_system_role_wins_over_a_legacy_role_when_both_are_present():
    assert policy.is_superuser(person(system_role="normal", role="superuser")) is False


def test_no_user_is_not_a_superuser():
    assert policy.is_superuser(None) is False


# --- has_entity_access and require_approved --------------------------------------------


@pytest.mark.django_db
def test_an_approved_member_has_access(user, entity):
    assert policy.has_entity_access(user, entity.id) is True


@pytest.mark.django_db
def test_an_unapproved_member_is_refused_by_default(user, entity):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(approved=False)
    assert policy.has_entity_access(user, entity.id) is False
    assert policy.has_entity_access(user, entity.id, require_approved=True) is False


@pytest.mark.django_db
def test_an_unapproved_member_passes_when_approval_is_not_required(user, entity):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(approved=False)
    assert policy.has_entity_access(user, entity.id, require_approved=False) is True


@pytest.mark.django_db
def test_a_non_member_has_no_access_either_way(other_user, entity):
    assert policy.has_entity_access(other_user, entity.id) is False
    assert policy.has_entity_access(other_user, entity.id, require_approved=False) is False


@pytest.mark.django_db
def test_a_superuser_has_access_with_no_row_at_all(entity):
    su = person(system_role="superuser")
    assert policy.has_entity_access(su, entity.id) is True
    assert policy.has_entity_membership(su, entity.id) is False  # and no row


@pytest.mark.parametrize("entity_id", [None, ""])
def test_no_entity_means_no_access(user, entity_id):
    assert policy.has_entity_access(user, entity_id) is False


def test_no_user_means_no_access():
    assert policy.has_entity_access(None, "some-entity") is False


# --- the superuser read-only rule -------------------------------------------------------


@pytest.mark.django_db
def test_a_superuser_on_a_customers_entity_is_read_only(entity):
    su = person(system_role="superuser")
    assert policy.is_superuser_readonly(su, entity.id) is True
    assert policy.resolve_effective_role(su, entity.id) == Role.SUPER_ADMIN.value

    for p in policy.READONLY_ALLOWED_PERMISSIONS:
        assert policy.has_permission(su, p, entity.id) is True, p
    # Every entity-scoped permission that is NOT a view is refused -- even though their
    # effective role resolves to super_admin.
    for p in Permission:
        rule = policy.PERMISSION_RULES.get(p)
        if not rule or not rule.entity_scoped or p in policy.READONLY_ALLOWED_PERMISSIONS:
            continue
        assert policy.has_permission(su, p, entity.id) is False, p


@pytest.mark.django_db
def test_a_superuser_with_a_row_on_the_entity_gets_full_access(entity):
    """Inside their OWN entity a superuser is an ordinary super_admin. The row is what
    distinguishes 'my company' from 'browsing a customer's'."""
    su = User.objects.create(
        id=str(uuid.uuid4()), email="su@example.com", username="su@example.com",
        password="x", system_role="superadmin", approved=True,
    )
    UserEntity.objects.create(user_id=su.id, entity_id=entity.id, role="super_admin", approved=True)
    assert policy.is_superuser_readonly(su, entity.id) is False
    assert policy.has_permission(su, Permission.ENTITY_UPDATE, entity.id) is True
    assert policy.has_permission(su, Permission.USER_INVITE, entity.id) is True


def test_read_only_never_applies_to_a_normal_user():
    assert policy.is_superuser_readonly(person(), "e") is False


def test_read_only_needs_an_entity():
    assert policy.is_superuser_readonly(person(system_role="superuser"), None) is False


# --- has_permission: the fail-closed edges ----------------------------------------------


@pytest.mark.django_db
def test_an_unknown_permission_is_refused_even_for_a_superuser(entity):
    # A typo must fail closed, never open.
    su = person(system_role="superuser")
    assert policy.has_permission(su, "not_a_permission", entity.id) is False
    assert policy.has_permission(su, None, entity.id) is False


@pytest.mark.django_db
def test_an_entity_scoped_permission_needs_an_entity(user):
    assert policy.has_permission(user, Permission.ENTITY_VIEW, None) is False
    assert policy.has_permission(user, Permission.ENTITY_VIEW, "") is False


def test_entity_create_needs_only_a_signed_in_person():
    # The whole premise of the wizard: there is no entity to be scoped to yet.
    assert policy.has_permission(person(), Permission.ENTITY_CREATE) is True
    assert policy.has_permission(None, Permission.ENTITY_CREATE) is False
    assert policy.has_permission(person(id=None), Permission.ENTITY_CREATE) is False


@pytest.mark.django_db
def test_an_unapproved_member_is_refused_every_entity_scoped_permission(user, entity):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(approved=False)
    for p in Permission:
        rule = policy.PERMISSION_RULES.get(p)
        if rule and rule.entity_scoped:
            assert policy.has_permission(user, p, entity.id) is False, p


@pytest.mark.django_db
def test_role_rank_is_applied(entity, user):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    assert policy.has_permission(user, Permission.REPORT_VIEW_OWN, entity.id) is True
    assert policy.has_permission(user, Permission.USER_INVITE, entity.id) is False  # shop_manager+


# --- role assignment: you may act on your rank and below, never below shop_manager -----


@pytest.mark.parametrize(
    "actor, target, allowed",
    [
        ("admin", "admin", True),
        ("admin", "cashier", True),
        ("admin", "super_admin", False),
        ("shop_manager", "shop_manager", True),
        ("shop_manager", "admin", False),
        ("cashier", "cashier", False),  # below the floor, even for their own rank
        ("accountant", "cashier", True),
    ],
)
def test_can_manage_role_assignment(actor, target, allowed):
    assert policy.can_manage_role_assignment(actor, target) is allowed


@pytest.mark.django_db
def test_can_manage_role_assignment_for_entity_uses_the_effective_role(user, entity):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="shop_manager")
    assert policy.can_manage_role_assignment_for_entity(user, "cashier", entity.id) is True
    assert policy.can_manage_role_assignment_for_entity(user, "admin", entity.id) is False
    assert policy.can_manage_role_assignment_for_entity(None, "cashier", entity.id) is False


# --- the door vs the room ---------------------------------------------------------------


@pytest.mark.django_db
def test_the_door_ignores_approval_and_the_room_does_not(user, entity):
    """The divergence, pinned. `is_member` mirrors Flask's `_entity_for_member`, which
    checks membership alone; `has_entity_access` enforces approval."""
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(approved=False)
    assert permissions.is_member(user.id, entity.id) is True
    assert policy.has_entity_access(user, entity.id) is False


@pytest.mark.django_db
def test_an_unapproved_member_can_still_resume(client, auth, user, entity, modules):
    """What the divergence buys: the resume works. The invite list -- which needs
    USER_VIEW_ALL -- comes back empty rather than failing the whole read."""
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(approved=False)
    resp = client.get(f"/api/onboarding/state?entity_id={entity.id}", **auth)
    assert resp.status_code == 200
    assert resp.json()["invites"] == []


@pytest.mark.django_db
def test_an_unapproved_member_is_refused_a_permission_gated_read(client, auth, user, entity):
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(approved=False)
    resp = client.get(f"/api/onboarding/invite?entity_id={entity.id}", **auth)
    assert resp.status_code == 403
