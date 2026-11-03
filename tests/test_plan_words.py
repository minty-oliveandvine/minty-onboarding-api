"""Step 2's price list reads ``billing_plan.code`` as the module codes themselves.

The rows said BILL / BILL+PETTY_CASH until 2026-11-03 (schema item 20, reversed) and
``plans.plan_modules`` mapped the word; every database is renamed and the mapping is gone.
"""

import uuid

import pytest
from minty_db.models import BillingPlan

PLANS = "/api/onboarding/plans"


def _seed(*rows) -> None:
    for code, name, amount in rows:
        BillingPlan.objects.create(
            id=uuid.uuid4(), code=code, display_name=name, amount=amount,
            currency="HKD", interval_months=1, is_active=True,
        )


@pytest.mark.django_db
def test_the_catalogue_speaks_module_codes(client, auth, currencies, modules, policy):
    _seed(("PETTY_CASH", "Petty Cash", 28000), ("PAYMENT_REQUEST", "Payment Request", 28000),
          ("PAYMENT_REQUEST+PETTY_CASH", "Super Minty", 40000))
    body = client.get(PLANS, **auth).json()
    assert sorted((p["code"], p["amount"]) for p in body["plans"]) == [
        ("PAYMENT_REQUEST", 280.0), ("PETTY_CASH", 280.0),
    ]
    assert body["bundle_codes"] == ["PAYMENT_REQUEST", "PETTY_CASH"]
    assert body["bundle_amount"] == 400.0
