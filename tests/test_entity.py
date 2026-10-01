"""Group C -- creating and editing the company.

The first group that writes, so the tests are weighted toward what a write can get wrong:
the resolvers that feed FK columns, idempotency on resume, and the presence-vs-truthiness
rule on the update path.

WHAT THESE CANNOT PROVE: that the writes actually land on Postgres. SQLite builds its tables
from the models, so a mirror missing a NOT NULL column passes here and fails in production --
which is exactly what happened with ``entity_function_map``. ``scripts/smoke_write.py``
covers that and is not optional.
"""

import os
import uuid

import pytest

from shared_models.models import (Entity, EntityFunctionMap, EntitySaleSetting,
                                  SaleInfo, UserEntity)
from tests.conftest import make_token

CREATE = "/api/onboarding/create"


def put_url(entity_id):
    return f"/api/onboarding/entity/{entity_id}"


def post_create(client, auth, **payload):
    return client.post(CREATE, payload, content_type="application/json", **auth)


def put_entity(client, auth, entity_id, **payload):
    return client.put(
        put_url(entity_id), payload, content_type="application/json", **auth
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_create_returns_201_with_the_new_id(client, auth, countries, currencies, modules):
    resp = post_create(client, auth, entity_name="Acme Trading", country_id="HK")
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Acme Trading"
    assert Entity.objects.filter(id=body["entity_id"], status="onboarding").exists()


@pytest.mark.django_db
def test_create_makes_the_caller_an_admin_member(client, auth, user, modules):
    entity_id = post_create(client, auth, entity_name="Acme").json()["entity_id"]
    membership = UserEntity.objects.get(user_id=user.id, entity_id=entity_id)
    # admin, not super_admin -- ported from Flask's "the creator becomes the entity admin
    # by policy". admin already clears every gate the wizard applies.
    assert membership.role == "admin"
    assert membership.approved is True


@pytest.mark.django_db
def test_create_requires_a_name(client, auth, modules):
    resp = post_create(client, auth, entity_name="   ")
    assert resp.status_code == 400
    assert resp.json() == {"error": "Entity name is required."}


@pytest.mark.django_db
def test_create_accepts_the_legacy_name_key(client, auth, modules):
    """``name`` is the older alias for ``entity_name``. Both still work."""
    resp = post_create(client, auth, name="Legacy Caller Co")
    assert resp.status_code == 201
    assert resp.json()["name"] == "Legacy Caller Co"


@pytest.mark.django_db
def test_a_taken_name_is_409_not_400(client, auth, modules):
    Entity.objects.create(id=str(uuid.uuid4()), name="Taken Co", status="disconnected")
    resp = post_create(client, auth, entity_name="Taken Co")
    assert resp.status_code == 409
    # Flask's exact wording, missing plural included -- the wizard matches on it.
    assert resp.json() == {"error": "Entity name already exist"}


@pytest.mark.django_db
def test_the_name_is_trimmed_before_the_uniqueness_check(client, auth, modules):
    Entity.objects.create(id=str(uuid.uuid4()), name="Trimmed Co", status="disconnected")
    assert post_create(client, auth, entity_name="  Trimmed Co  ").status_code == 409


# ---------------------------------------------------------------------------
# Idempotency -- the resume path
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_resubmitting_step_one_reuses_the_in_progress_entity(
    client, auth, user, modules
):
    """LOAD-BEARING, not a nicety.

    A cold resume has no localStorage, so a stale or racing wizard re-POSTs Step 1 for a
    company this user already created and abandoned. A second row would give them two
    half-finished companies with the same name and no way to tell which one the wizard is
    bound to.
    """
    first = post_create(client, auth, entity_name="Resumed Co").json()["entity_id"]
    second = post_create(client, auth, entity_name="Resumed Co")
    assert second.status_code == 201
    assert second.json()["entity_id"] == first
    assert Entity.objects.filter(name="Resumed Co").count() == 1


@pytest.mark.django_db
def test_reuse_does_not_duplicate_the_seeded_rows(client, auth, modules):
    entity_id = post_create(client, auth, entity_name="Once Co").json()["entity_id"]
    maps = EntityFunctionMap.objects.filter(entity_id=entity_id).count()
    methods = EntitySaleSetting.objects.filter(entity_id=entity_id).count()

    post_create(client, auth, entity_name="Once Co")

    assert EntityFunctionMap.objects.filter(entity_id=entity_id).count() == maps
    assert EntitySaleSetting.objects.filter(entity_id=entity_id).count() == methods


@pytest.mark.django_db
def test_idempotency_does_not_match_a_finalized_company(client, auth, user, modules):
    """DELIBERATELY NARROW: the key is (member, name, status='onboarding').

    If it matched a finalized company, re-submitting Step 1 would silently rebind the wizard
    to a live company and start editing it. The correct answer is the name conflict.
    """
    live = Entity.objects.create(id=str(uuid.uuid4()), name="Live Co", status="disconnected")
    UserEntity.objects.create(user_id=user.id, entity_id=live.id, role="admin")
    resp = post_create(client, auth, entity_name="Live Co")
    assert resp.status_code == 409


@pytest.mark.django_db
def test_idempotency_is_scoped_to_the_caller(client, auth, other_user, modules):
    """Another user's abandoned company is a name conflict, not something to adopt."""
    theirs = Entity.objects.create(
        id=str(uuid.uuid4()), name="Theirs Co", status="onboarding"
    )
    UserEntity.objects.create(user_id=other_user.id, entity_id=theirs.id, role="admin")
    resp = post_create(client, auth, entity_name="Theirs Co")
    assert resp.status_code == 409


@pytest.mark.django_db
def test_reuse_keeps_contact_details_the_wizard_did_not_resend(client, auth, modules):
    """A resumed wizard that omits the phone must not blank a stored one.

    The user typed it on a previous visit; an omitted key is not an instruction to delete.
    """
    entity_id = post_create(
        client, auth, entity_name="Keep Co", contact_phone="12345678"
    ).json()["entity_id"]
    post_create(client, auth, entity_name="Keep Co")
    assert Entity.objects.get(id=entity_id).contact_phone == "12345678"


# ---------------------------------------------------------------------------
# The module seed and its guard
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_creation_seeds_every_module_disabled(client, auth, modules):
    """Creation GRANTS NOTHING.

    Flask's create.py comment says "Petty Cash enabled, Bill disabled". That comment is
    stale -- DEFAULT_MODULE_STATE is both False, and the note above it records the cost of
    the old behaviour: "every entity ever created kept Petty Cash for free while its card
    still offered Start free trial".
    """
    entity_id = post_create(client, auth, entity_name="Seeded Co").json()["entity_id"]
    rows = EntityFunctionMap.objects.filter(entity_id=entity_id)
    assert rows.count() == 2
    assert all(row.is_enabled is False for row in rows)


@pytest.mark.django_db
def test_the_seed_writes_the_audit_columns(client, auth, user, modules):
    """The stamps are set, and ``created_by`` is the person who created the company
    (a uuid FK to ``user``, schema section 4) - never a label like 'entity_create'."""
    entity_id = post_create(client, auth, entity_name="Audited Co").json()["entity_id"]
    for row in EntityFunctionMap.objects.filter(entity_id=entity_id):
        assert row.created_at is not None
        assert row.updated_at is not None
        assert str(row.created_by) == str(user.id)


@pytest.mark.django_db
def test_the_guard_refuses_to_enable_a_module(db, entity):
    """THE NARROWED RULE, ENFORCED IN CODE.

    This service may write entity_function_map only with is_enabled=False. Enabling is a
    subscription write. The guard raises rather than coercing, so a future caller that tries
    fails loudly here instead of quietly granting a paid module for free.
    """
    from onboarding.services.entity_create import _seed_module_defaults

    with pytest.raises(AssertionError, match="may not enable a module"):
        _seed_module_defaults(entity.id, {"PETTY_CASH": True})


@pytest.mark.django_db
def test_a_missing_module_catalog_does_not_block_creation(client, auth):
    """No EntityFunction rows at all -- the seed migration has not run.

    Logged and tolerated rather than fatal: a company with no module rows is recoverable
    (the resolver denies every module, which is the safe answer), while refusing to create
    the company is not.
    """
    resp = post_create(client, auth, entity_name="No Catalog Co")
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Default sales methods
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_new_company_is_linked_to_the_eleven_default_methods(client, auth, modules):
    """Cash + seven electronic + three delivery, in that order; Cash is type 'other' keyed
    cash_sales (the closing-balance figure is found by that key)."""
    entity_id = post_create(client, auth, entity_name="Fresh Co").json()["entity_id"]
    links = list(
        EntitySaleSetting.objects.filter(entity_id=entity_id).select_related("sale")
        .order_by("sale__type", "display_order")
    )
    assert len(links) == 11
    by_type = {}
    for link in links:
        by_type.setdefault(link.sale.type, []).append(link.sale.sale_name)
    assert by_type["electronic"] == ["Visa", "Alipay", "WeChat Pay", "Mastercard", "UnionPay", "Amex", "Octopus"]
    assert by_type["delivery"] == ["Food Panda", "Keeta", "OpenRice"]
    cash = [l for l in links if l.sale.type == "other"]
    assert len(cash) == 1 and cash[0].sale.sale_name == "Cash" and cash[0].sale.value_name == "cash_sales"
    assert all(link.is_active for link in links)


@pytest.mark.django_db
def test_defaults_already_in_the_catalogue_are_linked_not_duplicated(client, auth, modules):
    """A catalogue that already has Visa (any spelling) gets no second Visa row."""
    visa = SaleInfo.objects.create(
        id=uuid.uuid4(), sale_name="VISA", type="electronic", value_name="visa_sales", enabled=True,
    )
    entity_id = post_create(client, auth, entity_name="Catalog Co").json()["entity_id"]
    assert SaleInfo.objects.filter(value_name="visa_sales").count() == 1
    assert EntitySaleSetting.objects.filter(entity_id=entity_id, sale=visa).exists()


@pytest.mark.django_db
def test_another_companys_custom_method_is_not_a_default(client, auth, modules, entity):
    """The catalogue is global, but a new company starts with the DEFAULT set only."""
    SaleInfo.objects.create(id=uuid.uuid4(), sale_name="Someone's Method", type="electronic", enabled=True)
    new_id = post_create(client, auth, entity_name="Other Co").json()["entity_id"]
    names = {l.sale.sale_name for l in EntitySaleSetting.objects.filter(entity_id=new_id).select_related("sale")}
    assert "Someone's Method" not in names
    assert len(names) == 11


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("value", ["HK", "hk", "HKG", "Hong Kong"])
def test_country_resolves_from_code_alpha3_or_name(client, auth, countries, modules, value):
    entity_id = post_create(
        client, auth, entity_name=f"Co {value}", country_id=value
    ).json()["entity_id"]
    assert Entity.objects.get(id=entity_id).country_code == "HK"


@pytest.mark.django_db
def test_an_unresolvable_country_is_a_400_not_a_null(client, auth, countries, modules):
    """Flask's note: skipping the assignment left country_code NULL while the currency
    still saved, so the two silently diverged and the 200 gave the wizard no way to
    notice."""
    resp = post_create(client, auth, entity_name="Bad Country Co", country_id="Atlantis")
    assert resp.status_code == 400
    assert resp.json() == {"error": "Unknown country: Atlantis"}
    assert not Entity.objects.filter(name="Bad Country Co").exists()


@pytest.mark.django_db
def test_an_ambiguous_country_prefix_resolves_to_nothing(client, auth, countries, modules):
    """The unique-prefix fallback must not guess between two countries."""
    from shared_models.models import CountryInfo

    CountryInfo.objects.create(
        country_code="AL", alpha3_code="ALB", country_name_en="Albania", is_active=True
    )
    # "A" now prefixes Australia, Afghanistan and Albania.
    resp = post_create(client, auth, entity_name="Ambiguous Co", country_id="A")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_a_like_wildcard_does_not_match_an_arbitrary_country(
    client, auth, countries, modules
):
    """``%`` must resolve to nothing.

    Flask escapes LIKE metacharacters by hand before its ``ilike`` calls. Django's
    ``istartswith`` escapes them itself, so the protection moved into the ORM rather than
    being dropped -- this test is what says so.
    """
    assert post_create(client, auth, entity_name="Wild Co", country_id="%").status_code == 400


@pytest.mark.django_db
def test_currency_resolves_from_uuid_or_iso_code(client, auth, currencies, modules):
    for i, value in enumerate([str(currencies["HKD"].id), "HKD", "hkd"]):
        entity_id = post_create(
            client, auth, entity_name=f"Cur Co {i}", currency_id=value
        ).json()["entity_id"]
        assert str(Entity.objects.get(id=entity_id).currency_id) == str(currencies["HKD"].id)


@pytest.mark.django_db
def test_a_malformed_uuid_is_a_400_and_not_a_crash(client, auth, currencies, modules):
    """On Postgres, comparing a uuid column to a bad literal aborts the transaction.

    So the resolver only queries the PK once the value is known to be a uuid. Without that
    guard a typo in this one field would take down the whole write.
    """
    resp = post_create(client, auth, entity_name="Bad Cur Co", currency_id="not-a-uuid")
    assert resp.status_code == 400
    assert resp.json() == {"error": "Unknown currency: not-a-uuid"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "phone,expected",
    [("12345678", "12345678"), ("+852 1234 5678", "85212345678"), ("", None)],
)
def test_phone_is_stored_digits_only(client, auth, modules, phone, expected):
    entity_id = post_create(
        client, auth, entity_name=f"Phone Co {phone or 'blank'}", contact_phone=phone
    ).json()["entity_id"]
    assert Entity.objects.get(id=entity_id).contact_phone == expected


@pytest.mark.django_db
@pytest.mark.parametrize("phone", ["1234567", "123456789012"])
def test_phone_length_is_enforced_server_side(client, auth, modules, phone):
    """The wizard's maxLength is a convenience, not a control -- this is plain JSON."""
    resp = post_create(client, auth, entity_name="Len Co", contact_phone=phone)
    assert resp.status_code == 400
    assert resp.json() == {"error": "Phone number must be 8-11 digits."}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "email", ["no-at-sign", "@nolocal.com", "nodomain@", "a b@c.com", "a@b@c.com", "nodot@domain"]
)
def test_business_email_rejects_the_obviously_broken(client, auth, modules, email):
    resp = post_create(client, auth, entity_name="Email Co", business_email=email)
    assert resp.status_code == 400
    assert resp.json() == {"error": "Please enter a valid business email."}


@pytest.mark.django_db
@pytest.mark.parametrize("email", ["김철수@walk.test", "hello@회사.한국", "café@walk.test"])
def test_business_email_is_english_only(client, auth, modules, email):
    """Printable ASCII only (2026-10-01), refused in the wizard's own words -- and before the
    company is created, like every contact-detail refusal."""
    resp = post_create(client, auth, entity_name="Korean Email Co", business_email=email)
    assert resp.status_code == 400
    assert resp.json() == {
        "error": "Email can only contain English letters, numbers and symbols."
    }
    assert not Entity.objects.filter(name="Korean Email Co").exists()


@pytest.mark.django_db
def test_update_holds_the_business_email_to_the_same_rule(client, auth, entity, modules):
    entity.business_email = "old@example.com"
    entity.save()

    resp = put_entity(client, auth, entity.id, business_email="김철수@walk.test")

    assert resp.status_code == 400
    assert resp.json() == {
        "error": "Email can only contain English letters, numbers and symbols."
    }
    entity.refresh_from_db()
    assert entity.business_email == "old@example.com"


@pytest.mark.django_db
def test_business_email_validation_is_deliberately_shallow(client, auth, modules):
    """One @ with something either side. The column never authenticates anybody and nothing
    is sent to it to confirm it, so a stricter parser would reject legitimate addresses for
    no gain."""
    resp = post_create(
        client, auth, entity_name="Odd Email Co", business_email="a+b@sub.domain.museum"
    )
    assert resp.status_code == 201


@pytest.mark.django_db
def test_contact_details_are_validated_before_the_company_is_created(client, auth, modules):
    """A bad phone must be a 400, not a half-saved company the user has to find again."""
    post_create(client, auth, entity_name="Atomic Co", contact_phone="123")
    assert not Entity.objects.filter(name="Atomic Co").exists()


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_update_edits_in_place_and_never_creates(client, auth, entity, countries, modules):
    before = Entity.objects.count()
    resp = put_entity(client, auth, entity.id, country_id="AU")
    assert resp.status_code == 200
    assert Entity.objects.count() == before
    assert Entity.objects.get(id=entity.id).country_code == "AU"


@pytest.mark.django_db
def test_update_requires_membership(client, other_user, entity):
    resp = client.put(
        put_url(entity.id),
        {"country_id": "AU"},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {make_token(other_user.id)}",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
@pytest.mark.skipif(
    bool(os.environ.get("MINTY_TEST_PG_URI")),
    reason="user_entity.entity_id is a real FK on the schema: a membership of a missing entity cannot exist",
)
def test_update_404s_for_a_member_of_a_missing_entity(client, auth, user):
    ghost = str(uuid.uuid4())
    UserEntity.objects.create(user_id=user.id, entity_id=ghost, role="admin")
    assert put_entity(client, auth, ghost, country_id="AU").status_code == 404


@pytest.mark.django_db
def test_an_empty_contact_field_clears_it(client, auth, entity, modules):
    """PRESENCE, NOT TRUTHINESS. "" means "clear this"."""
    entity.contact_phone = "12345678"
    entity.business_email = "old@example.com"
    entity.save()

    resp = put_entity(client, auth, entity.id, contact_phone="", business_email="")
    assert resp.status_code == 200
    entity.refresh_from_db()
    assert entity.contact_phone is None
    assert entity.business_email is None


@pytest.mark.django_db
def test_an_absent_contact_key_leaves_the_value_alone(client, auth, entity, modules):
    """The other half of the same rule -- and what keeps older callers working."""
    entity.contact_phone = "12345678"
    entity.save()
    put_entity(client, auth, entity.id, country_id="HK")
    entity.refresh_from_db()
    assert entity.contact_phone == "12345678"


@pytest.mark.django_db
def test_currency_is_derived_from_the_country_when_not_given(
    client, auth, entity, countries, currencies, modules
):
    """Picking a country alone still leaves the company with a currency."""
    from shared_models.models import CountryInfo

    CountryInfo.objects.filter(country_code="HK").update(currency_id=currencies["HKD"].id)
    resp = put_entity(client, auth, entity.id, country_id="HK")
    assert resp.status_code == 200
    entity.refresh_from_db()
    assert str(entity.currency_id) == str(currencies["HKD"].id)


@pytest.mark.django_db
def test_an_explicit_currency_beats_the_derived_one(
    client, auth, entity, countries, currencies, modules
):
    from shared_models.models import CountryInfo

    CountryInfo.objects.filter(country_code="HK").update(currency_id=currencies["HKD"].id)
    put_entity(client, auth, entity.id, country_id="HK", currency_id="JPY")
    entity.refresh_from_db()
    assert str(entity.currency_id) == str(currencies["JPY"].id)


@pytest.mark.django_db
def test_update_sets_currency_format_from_the_symbol(
    client, auth, entity, currencies, modules
):
    put_entity(client, auth, entity.id, currency_id="HKD")
    entity.refresh_from_db()
    assert entity.currency_format == "HK$"


@pytest.mark.django_db
def test_currency_format_falls_back_to_a_dollar_sign(client, auth, entity, currencies, modules):
    """JPY has no symbol in the fixture -- and no row in the dev database has one either.

    A blank currency_format renders as nothing, so "$" is the fallback. Flask does the same.
    """
    put_entity(client, auth, entity.id, currency_id="JPY")
    entity.refresh_from_db()
    assert entity.currency_format == "$"


@pytest.mark.django_db
def test_create_does_not_set_currency_format(client, auth, currencies, modules):
    """ASYMMETRY WITH UPDATE, ported rather than tidied.

    Update derives a currency and sets currency_format; create does neither. Making create
    match would change what a created row contains, and this port is not the place to decide
    that -- but it should be visible rather than discovered later.
    """
    entity_id = post_create(
        client, auth, entity_name="No Format Co", currency_id="HKD"
    ).json()["entity_id"]
    assert Entity.objects.get(id=entity_id).currency_format is None


# ---------------------------------------------------------------------------
# Renaming -- a second gate, above membership
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_an_admin_can_rename(client, auth, entity, modules):
    resp = put_entity(client, auth, entity.id, entity_name="Renamed Co")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Co"


@pytest.mark.django_db
def test_a_cashier_cannot_rename(client, user, entity, modules):
    """Membership gets you through the door; ENTITY_RENAME gets you this field.

    Re-checked here because a crafted PUT can carry a name the UI never offered.
    """
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    resp = client.put(
        put_url(entity.id),
        {"entity_name": "Sneaky Rename"},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {make_token(user.id)}",
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "You don't have permission to rename this entity"}
    entity.refresh_from_db()
    assert entity.name == "Wizard Trading Co"


@pytest.mark.django_db
def test_resending_the_same_name_is_not_a_rename(client, user, entity, modules):
    """A cashier resubmitting Step 1 unchanged must not be refused.

    The gate fires on a CHANGE, not on the key's presence -- otherwise every Step 1 save
    would 403 for anyone below admin.
    """
    UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).update(role="cashier")
    resp = client.put(
        put_url(entity.id),
        {"entity_name": "Wizard Trading Co"},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {make_token(user.id)}",
    )
    assert resp.status_code == 200


@pytest.mark.django_db
def test_renaming_to_a_taken_name_is_409(client, auth, entity, modules):
    Entity.objects.create(id=str(uuid.uuid4()), name="Other Co", status="disconnected")
    resp = put_entity(client, auth, entity.id, entity_name="Other Co")
    assert resp.status_code == 409
    assert resp.json() == {"error": "Entity name already exist"}


@pytest.mark.django_db
def test_renaming_is_not_blocked_by_the_entity_s_own_name(client, auth, entity, modules):
    """The uniqueness check excludes this entity, or no company could ever be re-saved."""
    resp = put_entity(client, auth, entity.id, entity_name="  Wizard Trading Co  ")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_renaming_to_blank_is_400(client, auth, entity, modules):
    resp = put_entity(client, auth, entity.id, entity_name="   ")
    assert resp.status_code == 400
    assert resp.json() == {"error": "Entity name is required."}


@pytest.mark.django_db
def test_an_over_long_name_is_400(client, auth, entity, modules):
    resp = put_entity(client, auth, entity.id, entity_name="x" * 101)
    assert resp.status_code == 400
    assert resp.json() == {"error": "Entity name must be 100 characters or fewer."}


@pytest.mark.django_db
def test_an_unresolvable_country_on_update_changes_nothing(
    client, auth, entity, countries, modules
):
    entity.country_code = "HK"
    entity.save()
    resp = put_entity(client, auth, entity.id, entity_name="Renamed", country_id="Atlantis")
    assert resp.status_code == 400
    entity.refresh_from_db()
    # The name assignment happens before the country check, but nothing was committed.
    assert entity.name == "Wizard Trading Co"
    assert entity.country_code == "HK"
