"""The database's enums, as this service must write and compare them.

Same members as Minty's ``blueprints/shared/enums.py`` (the register is item 18 of
``docs/schema/01_schema_rebased.sql`` in the Minty repo); a test there reads the schema
file and fails when any copy drifts. Use these instead of string literals — the columns
are Postgres enum types and reject any other word.
"""

from django.db import models


class SystemRole(models.TextChoices):
    """``system_role`` — the global role of a person. ``superadmin`` was ``superuser``
    in the code until 2026-09; old JWTs may still carry that word (see LEGACY_SUPERUSER)."""

    NORMAL = "normal"
    ADMIN = "admin"
    SUPERADMIN = "superadmin"


#: The pre-rename spelling of the system super admin; JWTs minted before C1 carry it.
#: Not ``admin``: that is a distinct, lesser member of the enum now.
LEGACY_SUPERUSER = frozenset({"superuser"})


def is_superadmin(system_role) -> bool:
    """Is this ``system_role`` value (a row's or a JWT claim's) the system super admin?"""
    word = (str(system_role or "")).strip().lower()
    return word == SystemRole.SUPERADMIN or word in LEGACY_SUPERUSER


class EntityRole(models.TextChoices):
    """``entity_role`` — a person's role within one company (``user_entity.role``)."""

    ENTITY_BASE = "entity_base"
    CASHIER = "cashier"
    SHOP_MANAGER = "shop_manager"
    ACCOUNTANT = "accountant"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"
