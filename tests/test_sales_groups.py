"""Step 5's sales methods are free lists again (owner, 2026-11-11), grouped by the business type:
Restaurant & Food takes Electronic and Delivery; a retail shop, a service or a general business takes
one Non-cash list (stored as electronic). A company with no type yet (from before the question)
keeps both. Step 4 no longer re-links them; a company with a mode edits them like any other.
"""

import pytest
from minty_db.models import Entity, EntitySaleSetting

from onboarding.services import sales_methods, state

SALES = "/api/onboarding/sales-methods"


def _set(client, auth, entity, electronic, delivery=()):
    return client.post(SALES, {"entity_id": str(entity.id), "electronic": list(electronic),
                               "delivery": list(delivery)}, content_type="application/json", **auth)


@pytest.mark.django_db
@pytest.mark.parametrize("business, expected", [
    ("fnb", "electronic_delivery"), (None, "electronic_delivery"),
    ("retail", "non_cash"), ("service", "non_cash"), ("general", "non_cash"),
])
def test_the_groups_follow_the_business_type(client, auth, entity, business, expected):
    Entity.objects.filter(id=entity.id).update(business_type=business)
    assert sales_methods.groups(entity.id) == expected
    assert client.get(SALES, {"entity_id": str(entity.id)}, **auth).json()["groups"] == expected


@pytest.mark.django_db
def test_a_company_with_a_mode_edits_its_methods(client, auth, entity):
    Entity.objects.filter(id=entity.id).update(setup_mode="minty", business_type="retail", uses_sales_cash=True)
    resp = _set(client, auth, entity, ["Visa", "Octopus"])
    assert resp.status_code == 200, resp.content
    active = set(EntitySaleSetting.objects.filter(entity_id=entity.id, is_active=True)
                 .values_list("sale__sale_name", flat=True))
    assert active == {"Visa", "Octopus"}
    assert not EntitySaleSetting.objects.filter(entity_id=entity.id, account_id__isnull=False).exists(), (
        "no account link any more"
    )


@pytest.mark.django_db
def test_resume_carries_the_groups(entity):
    Entity.objects.filter(id=entity.id).update(business_type="general")
    assert state._sales_methods_state(str(entity.id))["groups"] == "non_cash"
