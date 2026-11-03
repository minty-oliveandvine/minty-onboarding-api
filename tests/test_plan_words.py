"""Step 2's price list is the same whichever plan word ``billing_plan.code`` holds.

The rows are being renamed from BILL / BILL+PETTY_CASH to PAYMENT_REQUEST /
PAYMENT_REQUEST+PETTY_CASH (schema item 20, reversed 2026-11-03), and until November
development and production read the same rows. ``plans.plan_modules`` reads both, so the
catalogue must not change when the rows do. Drop the BILL half once every database is renamed.
"""

import uuid

import pytest
from minty_db.models import BillingPlan

PLANS = "/api/onboarding/plans"


def _seed(payment_request: str, bundle: str) -> None:
    for code, name, amount in (
        ("PETTY_CASH", "Petty Cash", 28000),
        (payment_request, "Payment Request", 28000),
        (bundle, "Super Minty", 40000),
    ):
        BillingPlan.objects.create(
            id=uuid.uuid4(), code=code, display_name=name, amount=amount,
            currency="HKD", interval_months=1, is_active=True,
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payment_request", "bundle"),
    [("BILL", "BILL+PETTY_CASH"), ("PAYMENT_REQUEST", "PAYMENT_REQUEST+PETTY_CASH")],
    ids=["bill-rows", "payment-request-rows"],
)
def test_the_catalogue_reads_either_spelling(client, auth, currencies, modules, policy,
                                             payment_request, bundle):
    _seed(payment_request, bundle)
    body = client.get(PLANS, **auth).json()
    assert sorted((p["code"], p["amount"]) for p in body["plans"]) == [
        ("PAYMENT_REQUEST", 280.0), ("PETTY_CASH", 280.0),
    ]
    assert body["bundle_codes"] == ["PAYMENT_REQUEST", "PETTY_CASH"]
    assert body["bundle_amount"] == 400.0
