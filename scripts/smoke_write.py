"""Exercise every WRITE against the real Postgres schema, then roll it all back.

WHY THE TEST SUITE CANNOT DO THIS

pytest runs on SQLite and builds its tables FROM the models. So a mirror that omits a
column simply has no such column in the test database, the insert succeeds, and the suite is
green -- while the same insert against Postgres fails with a not-null violation.

That is not hypothetical. ``entity_function_map.created_at`` and ``updated_at`` are NOT NULL
with no database default, they were missing from the mirror, and nothing but a real write
against the real schema would have found it.

Worse than a missing column is a WRONG DEFAULT. ``entity_function_map.is_enabled`` defaults
to ``true`` in the database, so a row inserted without naming it grants the module. A test on
SQLite, whose column default comes from the model, would report ``False`` and pass.

So: this script performs the real writes, asserts what actually landed, and rolls back.

    .venv/Scripts/python scripts/smoke_write.py

Everything runs inside one transaction that is always rolled back, so it is safe against a
database with real data in it -- nothing it creates survives the process. It still WRITES,
though, so do not point it at production.
"""

import os
import sys
import uuid

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connection, transaction  # noqa: E402

from onboarding.services import opening_balance as opening_service  # noqa: E402
from onboarding.services import resolve  # noqa: E402
from onboarding.services import sales_methods as sales_service  # noqa: E402
from onboarding.services.entity_create import create_entity_for_user  # noqa: E402
from shared_models.models import (CountryInfo, CurrencyInfo,  # noqa: E402
                                  EntityFunctionMap, EntitySaleSetting, Report,
                                  User, UserEntity)

PASS = "  PASS  "
FAIL = "  FAIL  "


class Checks:
    def __init__(self):
        self.failed = 0

    def ok(self, label, condition, detail=""):
        if condition:
            print(f"{PASS}{label}")
        else:
            self.failed += 1
            print(f"{FAIL}{label}" + (f"\n          {detail}" if detail else ""))


def main():
    checks = Checks()

    user_id = User.objects.order_by("id").values_list("id", flat=True).first()
    if not user_id:
        print("No users in the database -- cannot smoke-test a write.")
        return 2

    country = CountryInfo.objects.filter(is_active=True).order_by("country_code").first()
    currency = CurrencyInfo.objects.filter(is_active=True).order_by("currency_code").first()

    print(f"Database  {connection.settings_dict['NAME']} (schema pettycashv2)")
    print(f"Actor     user={user_id}")
    print("All writes roll back at the end.\n")

    # A name nothing can collide with, so a leftover row from an aborted run cannot make
    # this report a false conflict.
    name = f"zz-smoke-{uuid.uuid4().hex[:12]}"

    try:
        with transaction.atomic():
            # --- resolvers, against the real registries ---------------------------
            checks.ok(
                "resolve.country_code accepts the alpha-2 PK",
                resolve.country_code(country.country_code) == country.country_code,
            )
            checks.ok(
                "resolve.country_code accepts the alpha-3 code",
                resolve.country_code(country.alpha3_code) == country.country_code,
            )
            checks.ok(
                "resolve.country_code accepts the exact name",
                resolve.country_code(country.country_name_en) == country.country_code,
            )
            checks.ok(
                "resolve.country_code refuses an unknown value",
                resolve.country_code("Notacountry") == "",
            )
            checks.ok(
                "resolve.country_code refuses a bare LIKE wildcard",
                resolve.country_code("%") == "",
                "a wildcard that resolved would match an arbitrary country",
            )
            checks.ok(
                "resolve.currency_id accepts the uuid PK",
                resolve.currency_id(str(currency.id)) == str(currency.id),
            )
            checks.ok(
                "resolve.currency_id accepts the ISO code",
                resolve.currency_id(currency.currency_code) == str(currency.id),
            )
            checks.ok(
                "resolve.currency_id survives a malformed uuid",
                resolve.currency_id("not-a-uuid") == "",
                "on Postgres a bad uuid literal aborts the transaction if not guarded",
            )

            # --- the create itself -------------------------------------------------
            entity, created = create_entity_for_user(
                user_id, name, country.country_code, str(currency.id)
            )
            checks.ok("create_entity_for_user reports created", created is True)
            checks.ok("entities row written", entity.id is not None)
            checks.ok("status is 'onboarding'", entity.status == "onboarding")
            checks.ok(
                "country_code and currency_id stored",
                entity.country_code == country.country_code
                and str(entity.currency_id) == str(currency.id),
                f"got country={entity.country_code!r} currency={entity.currency_id!r}",
            )

            membership = UserEntity.objects.filter(
                user_id=str(user_id), entity_id=entity.id
            ).first()
            checks.ok("user_entity row written", membership is not None)
            checks.ok(
                "creator role is admin",
                membership is not None and membership.role == "admin",
            )

            methods = list(EntitySaleSetting.objects.filter(entity_id=entity.id))
            checks.ok("default sales methods seeded", len(methods) > 0, f"got {len(methods)}")
            checks.ok(
                "a Cash method exists and is typed 'Cash'",
                any(m.type == "Cash" for m in methods),
                "the closing-balance figure is found by keying on this type",
            )

            # --- THE ONE THIS SCRIPT EXISTS FOR ------------------------------------
            maps = list(EntityFunctionMap.objects.filter(entity_id=entity.id))
            checks.ok(
                "entity_function_map rows written (NOT NULL audit columns present)",
                len(maps) > 0,
                "a not-null violation here means the mirror is missing created_at/updated_at",
            )
            checks.ok(
                "every seeded module row is DISABLED",
                all(m.is_enabled is False for m in maps),
                "the column's DATABASE default is true -- an omitted value grants the module",
            )
            checks.ok(
                "audit columns populated",
                all(m.created_at and m.updated_at for m in maps),
            )
            checks.ok(
                "created_by records the actor",
                all(m.created_by == "entity_create" for m in maps),
                f"got {sorted({m.created_by for m in maps})}",
            )

            # --- idempotency, the resume path --------------------------------------
            again, created_again = create_entity_for_user(
                user_id, name, country.country_code, str(currency.id)
            )
            checks.ok("a repeated Step 1 submit reuses the entity", again.id == entity.id)
            checks.ok("and reports created=False", created_again is False)
            checks.ok(
                "no duplicate module rows after the retry",
                EntityFunctionMap.objects.filter(entity_id=entity.id).count() == len(maps),
            )

            # --- the guard ----------------------------------------------------------
            from onboarding.services.entity_create import _seed_module_defaults

            try:
                _seed_module_defaults(entity.id, {"PETTY_CASH": True})
                checks.ok("the module-grant guard refuses True", False, "it did NOT raise")
            except AssertionError:
                checks.ok("the module-grant guard refuses True", True)

            # --- Group D1: sales methods (reconcile) --------------------------------
            sales_service.replace(user_id, entity.id, ["Visa", "Tap & Go"], ["Keeta"])
            after = sales_service.list_grouped(user_id, entity.id)
            checks.ok(
                "sales methods reconcile to the submitted lists",
                after == {"electronic": ["Visa", "Tap & Go"], "delivery": ["Keeta"]},
                f"got {after}",
            )
            checks.ok(
                "a user-typed method got a catalog row",
                EntitySaleSetting.objects.filter(
                    entity_id=entity.id, sale_name="Tap & Go"
                ).exclude(sale_info_id=None).exists(),
                "without it the method has no storable column and no catalog link",
            )

            sales_service.replace(user_id, entity.id, ["Visa"], [])
            removed = EntitySaleSetting.objects.filter(
                entity_id=entity.id, sale_name="Keeta"
            ).first()
            checks.ok(
                "a removed method is soft-disabled, not deleted",
                removed is not None and removed.enabled is False,
                "report_sale_detail rows still reference it",
            )
            checks.ok(
                "Cash survives a Sales Setting save",
                EntitySaleSetting.objects.filter(
                    entity_id=entity.id, type="Cash", enabled=True
                ).exists(),
                "Cash is not a managed type on this screen",
            )

            # --- Group D1: the opening draft ---------------------------------------
            from datetime import date, timedelta

            day_one = date.today() - timedelta(days=3)
            seeded = opening_service.seed_opening_draft(
                user_id, entity.id, day_one.isoformat(), 1234.5
            )
            checks.ok("opening draft created", seeded["created"] is True)

            draft = Report.objects.filter(company=entity.id, status="draft").first()
            checks.ok(
                "report row written (NOT NULL sales/deposit columns present)",
                draft is not None,
                "a not-null violation here means the Report mirror is missing a column",
            )
            checks.ok(
                "the amount is an opening balance, not a cash addition",
                draft is not None
                and draft.opening_balance == 1234.5
                and draft.cash_addition == 0.0,
                f"got opening={getattr(draft, 'opening_balance', None)} "
                f"addition={getattr(draft, 'cash_addition', None)}",
            )
            checks.ok(
                "completed_sections round-trips through a Postgres json column",
                draft is not None and draft.completed_sections == [],
                "a plain `json` column (not jsonb) is pre-parsed by psycopg2 -- "
                "see TolerantJSONField",
            )

            day_two = date.today() - timedelta(days=1)
            moved = opening_service.seed_opening_draft(
                user_id, entity.id, day_two.isoformat(), 99.0
            )
            checks.ok("re-saving moves the same draft", moved["created"] is False)
            checks.ok(
                "no second draft was left behind",
                Report.objects.filter(company=entity.id, status="draft").count() == 1,
            )

            raise _Rollback()

    except _Rollback:
        pass

    print("\nRolled back. Nothing written survives.")
    print(f"{'FAILED' if checks.failed else 'OK'} -- {checks.failed} check(s) failed")
    return 1 if checks.failed else 0


class _Rollback(Exception):
    """Raised to unwind the atomic block. Postgres rolls back; nothing persists."""


if __name__ == "__main__":
    raise SystemExit(main())
