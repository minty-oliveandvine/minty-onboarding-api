"""Django mirrors of tables the Flask app's Alembic migrations own.

EVERY MODEL HERE IS ``managed = False`` AND THIS REPO SHIPS NO MIGRATIONS.

Alembic in Minty is the owner-of-record for all of pettycashv3. A new column means
a migration there first, then a hand-edit here. That hand-sync is a real cost --
billing-backend's equivalent file carries a docstring naming the Flask module and
the Alembic revision that owns one column, because the drift is tracked by hand --
but it is the established convention, and inventing a second DDL owner is worse.

WHAT THIS SERVICE MAY WRITE

Writable:  entities, user_entity, entity_sale_setting
Write-restricted: entity_function_map -- ONLY with is_enabled=False (see below)
Read-only: user, country_info, currency_info, sale_info, entity_function,
           billing_plan, billing_policy, report, entity_pettycash_settings, invitations
           (the last three become writable with Groups D and E)

``entity_function_map`` is read-only ON PURPOSE, and it is the one that looks like it
should not be. Onboarding Step 2 picks modules, so writing the module map from here is
the obvious move -- but ``is_enabled`` on that row is not a fact of its own. It is a
projection of ``entity_module_subscription``, which the subscription lifecycle writes
(billing-backend/core/entitlements.py says so, and Flask's
blueprints/entity/services/modules.py declares itself "the single place that writes to
them"). A module is granted by a trial or a subscription starting, never by a wizard
step. So POST /api/onboarding/modules stays in Flask.

THE ONE EXCEPTION, AND WHY IT IS NOT A HOLE

Creating an entity has to seed ``entity_function_map`` with a row per module, all OFF.
That seed is not a grant -- it exists to CLOSE a permissive hole. Flask's comment above
``DEFAULT_MODULE_STATE`` records what the hole cost: with no explicit row, the resolver
fell back to the catalog's ``is_active`` (default True), so "every entity ever created
kept Petty Cash for free while its card still offered Start free trial -- the two read
different tables".

So the rule is narrowed rather than broken: this service may write
``entity_function_map`` ONLY with ``is_enabled=False``. Never True. That keeps the
substance intact -- a module is still granted only by the subscription lifecycle -- while
letting creation write the safe default. ``onboarding/services/entity_create.py`` enforces
it with a guard that raises rather than trusting the caller, so the boundary is mechanical
and not a convention someone has to remember.

DEFAULT TRAP: A SQLALCHEMY ``default=`` IS INVISIBLE TO DJANGO

This one reached production, so read it before adding any write.

SQLAlchemy distinguishes ``default=`` (applied in PYTHON, by SQLAlchemy, on insert) from
``server_default=`` (a real DDL default). Django can only ever see the second. So a column
Flask "always fills" may have no database-level default at all, and a Django insert that
omits it stores NULL.

``report.date`` is exactly that: ``db.Column(db.DateTime, default=lambda: datetime.now(tz))``,
no server default. A draft created here without it stored NULL, and Minty's Select Company
page -- which does ``datetime.now() - report.date`` with no guard -- died with a TypeError on
the whole page for that user.

NEITHER the test suite NOR the parity sweep could catch it: SQLite builds its tables from
these models so the insert succeeded, and parity only compares READS. Only a real write
followed by a real read from the other service would have shown it, which is what
``scripts/smoke_write.py`` now asserts.

So: before writing a table, check the Flask model for ``default=`` (without ``server_``) on
every column, and fill each one explicitly. A row this service writes must be
indistinguishable from a row Flask writes.

TYPE TRAP: uuid columns come back as ``uuid.UUID``, not ``str``

``entities.currency_id``, ``currency_info.id`` and ``billing_plan.id`` are Postgres
``uuid``. Flask declares them ``UUID(as_uuid=False)`` so SQLAlchemy hands back ``str``
and its JSON carries a plain string. Django's ``UUIDField`` hands back a ``UUID``
object, which serialises differently and compares unequal to the string the wizard
sends. Every response builder must ``str()`` these, and every lookup must accept both.
See ``onboarding/api_reference.py`` and ``services/state.py`` for the call sites that do it.
"""

from django.db import models
from django.db.models.functions import Now

from shared_models.enums import (DiscrepancyType, EntityRole, EntityStatus, ModuleCode, PublishStatus,
                                 ReportStatus, SaleType, SystemRole)
from shared_models.fields import CharNField, PgEnumField


class TolerantJSONField(models.JSONField):
    """``JSONField`` for a Postgres ``json`` column, as opposed to ``jsonb``.

    Django's Postgres backend neutralises psycopg2's automatic parsing for ``jsonb`` only
    (``register_default_jsonb(loads=lambda x: x)``), because ``JSONField.from_db_value``
    wants to do the decoding itself. Plain ``json`` columns get no such treatment, so
    psycopg2's default typecaster parses them first and ``JSONField`` then calls
    ``json.loads`` on an already-decoded list:

        TypeError: the JSON object must be str, bytes or bytearray, not list

    ``report.completed_sections`` is declared ``db.JSON`` in Flask, so it is a ``json``
    column and hits exactly this. The failure is a 500 on READ -- and only for entities that
    actually have a draft, which is why it survived the unit suite (SQLite stores JSON as
    text and decodes it the way Django expects) and only appeared in the parity sweep, on 4
    of 14 real entities.

    Pass the value through when the driver has already decoded it; otherwise defer to
    Django. Contained to this field rather than re-registering a global psycopg2 typecaster,
    which would change behaviour for every ``json`` column in the schema -- including ones
    this service does not own.
    """

    def from_db_value(self, value, expression, connection):
        if value is None or isinstance(value, (str, bytes, bytearray)):
            return super().from_db_value(value, expression, connection)
        return value


# ---------------------------------------------------------------------------
# People and companies
# ---------------------------------------------------------------------------
class User(models.Model):
    """Read-only mirror of pettycashv3.user, managed by the Flask app.

    Onboarding never writes a user: Flask owns registration, the email-OTP login and
    the ``itsdangerous`` session handoff. This exists so a verified JWT's ``user_id``
    can be resolved to a real person, and so the wizard can show their name.

    Deliberately omits the Xero OAuth token columns (``access_token``,
    ``refresh_token``, ``id_token``, ``expires_in``, ``token_created_at``) that
    billing-backend's mirror carries. Nothing here may use a Xero token -- Flask is the
    sole refresher -- and a column that must not be read is better absent than present.
    """

    id = models.UUIDField(primary_key=True)
    email = models.CharField(max_length=254, unique=True, null=True, blank=True)
    # Present so a test-mode insert satisfies the NOT NULL; never read here (Flask checks it).
    password = models.CharField(max_length=255)
    first_name = models.CharField(max_length=150, default="")
    last_name = models.CharField(max_length=150, default="")
    username = models.CharField(max_length=150, unique=True)
    system_role = PgEnumField("system_role", choices=SystemRole.choices, default=SystemRole.NORMAL)
    is_active = models.BooleanField(default=True)
    approved = models.BooleanField(default=False)
    # NOT NULL DEFAULT now() in the schema; db_default lets an insert leave them to Postgres.
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())
    # xero_entity_id is gone: which company a person connected is entities.connected_by_user_id

    class Meta:
        managed = False
        db_table = "user"

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"


class Entity(models.Model):
    """The company. WRITABLE -- onboarding creates and edits it.

    ``status`` is the ``entity_status`` enum: 'onboarding' for the whole wizard, then
    'connected' / 'disconnected' (a Xero org linked or not) from finalize on - finalize
    stays in Flask, because it also starts the trial.

    ``onboarding_saved_step`` is the only column in the whole schema this service
    genuinely owns. It holds the FRONTEND step id (1-9) verbatim, not the backend's
    derived ``current_step`` ordering -- the two orderings differ, and conflating them
    lands the user on the wrong step.
    """

    id = models.UUIDField(primary_key=True)
    name = models.CharField(max_length=100)
    # FKs into the registries, as plain columns: country_code is the ISO alpha-2
    # country_info PK; currency_id is a uuid into currency_info(id). Kept as scalar
    # fields rather than Django ForeignKeys so a write never needs the related row loaded.
    country_code = CharNField(max_length=2, null=True, blank=True)
    currency_id = models.UUIDField(null=True, blank=True)
    # Step 1 contact details for the COMPANY, not the person who signed up
    # (user.email is that, and one user can own several entities). Both optional.
    # The phone is stored digits-only.
    contact_phone = models.CharField(max_length=36, null=True, blank=True)
    business_email = models.CharField(max_length=100, null=True, blank=True)
    xero_org_id = models.CharField(max_length=36, null=True, blank=True)
    xero_tenant_name = models.CharField(max_length=255, null=True, blank=True)
    currency_format = models.CharField(max_length=30, null=True, blank=True)
    timezone = models.CharField(max_length=30, null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    status = PgEnumField("entity_status", choices=EntityStatus.choices, default=EntityStatus.ONBOARDING)
    onboarding_saved_step = models.IntegerField(null=True, blank=True)
    financial_year_end_day = models.SmallIntegerField(null=True, blank=True)
    financial_year_end_month = models.SmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())
    last_connected_at = models.DateTimeField(null=True, blank=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    last_accessed_by_user_id = models.UUIDField(null=True, blank=True)
    connected_by_user_id = models.UUIDField(null=True, blank=True)
    # minimum_qty / deposit_frequency / deposit_day / xero_short_code and the two Xero lock
    # dates are gone with the schema redesign (item 15).

    class Meta:
        managed = False
        db_table = "entities"

    def __str__(self):
        return self.name


class UserEntity(models.Model):
    """Membership. WRITABLE -- onboarding adds the creator as ``admin``.

    COMPOSITE PRIMARY KEY, modelled as one. billing-backend's mirror declares
    ``user = OneToOneField(..., primary_key=True)``, which tells Django a user belongs
    to exactly ONE entity. That is false -- a user creates several companies and gets a
    row per company -- and it is harmless there only because that service never inserts
    into this table. This one does, so the key is declared as it actually is.

    ``CompositePrimaryKey`` needs Django 5.2; requirements.txt pins for it.
    """

    pk = models.CompositePrimaryKey("user_id", "entity_id")
    user_id = models.UUIDField()
    entity_id = models.UUIDField()
    role = PgEnumField("entity_role", choices=EntityRole.choices)
    approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(db_default=Now())
    joined_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "user_entity"

    def __str__(self):
        return f"{self.user_id}@{self.entity_id} ({self.role})"


# ---------------------------------------------------------------------------
# Reference registries -- read-only, seeded by Flask
# ---------------------------------------------------------------------------
class CountryInfo(models.Model):
    """Read-only. ISO 3166-1 alpha-2 ``country_code`` is the primary key.

    Seeded with the full ~250-row list; ``is_active`` narrows it to the countries this
    deployment operates in. ``display_order`` defaults to 999 so common countries can be
    floated above the alphabetical tail.
    """

    country_code = CharNField(max_length=2, primary_key=True)
    alpha3_code = CharNField(max_length=3)
    country_name_en = models.CharField(max_length=100)
    currency_id = models.UUIDField(null=True, blank=True)
    phone_code = models.CharField(max_length=10, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(default=999)

    class Meta:
        managed = False
        db_table = "country_info"

    def __str__(self):
        return f"{self.country_code} {self.country_name_en}"


class CurrencyInfo(models.Model):
    """Read-only. ``id`` is a uuid PK; ``currency_code`` is the unique ISO 4217 code.

    ``symbol`` and ``decimal_places`` are what money formatting resolves against -- the
    symbol is never hardcoded anywhere, here or in Flask.
    """

    id = models.UUIDField(primary_key=True)
    currency_code = CharNField(max_length=3, unique=True)
    currency_name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=10, default="")
    decimal_places = models.SmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "currency_info"

    def __str__(self):
        return f"{self.currency_code} ({self.currency_name})"


# ---------------------------------------------------------------------------
# Module catalog and gate -- READ-ONLY, see the module header
# ---------------------------------------------------------------------------
class EntityFunction(models.Model):
    """Read-only. The module catalog: which modules exist at all.

    ``is_active`` says whether a module is OFFERED, never who may use it. Flask deleted
    a fallback that read it as permission, and billing-backend's entitlements.py repeats
    the warning, because falling back to it hands a module to every entity that never
    subscribed.
    """

    id = models.UUIDField(primary_key=True)
    function_code = PgEnumField("module_code", choices=ModuleCode.choices, unique=True)
    function_name = models.CharField(max_length=150)
    description = models.TextField(db_default="")
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(db_default=999)

    class Meta:
        managed = False
        db_table = "entity_function"

    def __str__(self):
        return self.function_code


class EntityFunctionMap(models.Model):
    """Read-only. Per-entity module on/off -- a projection of subscription state.

    Read here only to derive the wizard's resume step. See the module header for why
    nothing in this service writes it.
    """

    # Keyed by (entity_id, entity_function_id) - the schema has no surrogate id.
    pk = models.CompositePrimaryKey("entity_id", "entity_function_id")
    entity_id = models.UUIDField(db_index=True)
    entity_function_id = models.UUIDField()
    # DANGER: the DATABASE default for this column is `true`.
    #
    # A row inserted without naming is_enabled GRANTS the module. Every write here must
    # set it explicitly -- which is also why `default=False` is set on the field rather
    # than mirroring the database. The model default is the safer one on purpose: if a
    # future caller forgets the keyword, it fails closed instead of handing out a paid
    # module. See onboarding/services/entity_create.py::_seed_module_defaults.
    is_enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    # The person who first wrote the row (uuid FK to user, schema section 4) - the wizard
    # user here; NULL for a job or the CLI. The reason (onboarding / cli) is not stored.
    created_by = models.UUIDField(null=True, blank=True)
    # NOT NULL DEFAULT now() in the schema; written explicitly here so a row's stamps are
    # the same instant as its siblings'.
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_function_map"

    def __str__(self):
        return f"{self.entity_id} -> {self.entity_function_id} ({self.is_enabled})"


# ---------------------------------------------------------------------------
# Price catalog -- read-only. NOT Stripe.
# ---------------------------------------------------------------------------
class BillingPlan(models.Model):
    """Read-only. Minty's own price catalog -- the prices live HERE, not in Stripe.

    Worth stating plainly, because the Flask docstring that fronts this data still says
    "live from Stripe" and is stale: ``subscription/services/catalog.py`` replaced the
    code that read Stripe Products and Prices, imports no stripe module, and reads this
    table. So GET /api/onboarding/plans is a plain database read and belongs in this
    service rather than behind a proxy.

    ``code`` is the module SET this plan bills, upper-cased, sorted, joined with '+':
    'BILL', 'PETTY_CASH', 'BILL+PETTY_CASH'. A code containing '+' is the bundle, and
    the bundle IS the discount. Sorting is what keeps the key canonical.

    ``amount`` is integer MINOR units (40000 == HKD 400.00), never a float.

    Rows describe the price NOW. An issued invoice carries its amount by value, so never
    reconstruct a historical invoice from this table.
    """

    id = models.UUIDField(primary_key=True)
    code = models.CharField(max_length=200, unique=True)
    display_name = models.CharField(max_length=200)
    amount = models.IntegerField()
    currency = models.CharField(max_length=3)
    interval_months = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "billing_plan"

    def __str__(self):
        return f"{self.code} {self.amount} {self.currency}"


class BillingPolicy(models.Model):
    """Read-only singleton (``id`` is always 1). Commercial policy, not arithmetic.

    Onboarding reads exactly one field, ``trial_days``, to tell Step 2 how long the
    card-free trial runs. The rest is here so the row is legible to a reader, and so a
    future read does not need a schema change -- not because this service uses it.

    These windows are tunable on purpose (they used to be constants). Do not treat the
    shipped defaults as fixed.
    """

    id = models.IntegerField(primary_key=True)
    trial_days = models.IntegerField(default=30)
    paid_cancel_access_days = models.IntegerField(default=30)
    past_due_window_days = models.IntegerField(default=15)
    retry_offsets_days = models.CharField(max_length=100, default="")
    updated_at = models.DateTimeField(db_default=Now())  # NOT NULL DEFAULT now()
    updated_by = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        managed = False
        db_table = "billing_policy"

    def __str__(self):
        return f"<BillingPolicy trial={self.trial_days}d>"


# ---------------------------------------------------------------------------
# Group B reads: petty-cash config, sales methods, the opening draft, invites
#
# Writable from Group D/E onward; read-only for now, since Group B only derives
# the wizard's resume step from them.
# ---------------------------------------------------------------------------
class EntityPettycashSettings(models.Model):
    """Per-entity Xero account mappings for the petty-cash module (``entity_pettycash_settings``).

    ``entity_id`` is the primary key -- one row per entity, not an id column. The account
    and contact columns are FKs to the company's synced ``account_info`` /
    ``xero_contact_sync`` rows (uuids); Group B reads whether ``pettycash_account_id`` is
    set, which is what marks Step 6 (account codes) as done, and Group D writes the six
    account columns.
    """

    entity_id = models.UUIDField(primary_key=True)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    pettycash_account_id = models.UUIDField(null=True, blank=True)
    bank_account_id = models.UUIDField(null=True, blank=True)
    cash_sale_account_id = models.UUIDField(null=True, blank=True)
    discrepancy_bank_account_id = models.UUIDField(null=True, blank=True)
    discrepancy_account_id = models.UUIDField(null=True, blank=True)
    director_account_id = models.UUIDField(null=True, blank=True)
    cash_sale_contact_id = models.UUIDField(null=True, blank=True)
    director_contact_id = models.UUIDField(null=True, blank=True)
    discrepancy_contact_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "entity_pettycash_settings"

    def __str__(self):
        return f"pettycash settings for {self.entity_id}"


class EntitySaleSetting(models.Model):
    """Which sales methods a company uses: a link from the company to a catalogue row
    (``SaleInfo``), keyed ``(entity_id, sale_id)``, carrying only what is per-company - on/off
    and the order on the sales page. Name, type and form-field name are the catalogue's.

    Step 5 of the wizard reads and reconciles these rows (onboarding/services/sales_methods).
    """

    pk = models.CompositePrimaryKey("entity_id", "sale_id")
    entity_id = models.UUIDField()
    sale = models.ForeignKey(
        "SaleInfo", on_delete=models.DO_NOTHING, db_column="sale_id", db_constraint=False,
        related_name="entity_links",
    )
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "entity_sale_setting"

    def __str__(self):
        return f"{self.sale_id} for {self.entity_id} ({'on' if self.is_active else 'off'})"


class Report(models.Model):
    """The petty-cash report (``report``). Mirrored for ONE row: the opening draft.

    Group B reads the entity's earliest ``status='draft'`` row to rehydrate the opening
    balance the wizard captured at Step 5, and writes it. Since C4 the columns are the
    schema's: ``entity_id`` (was ``company``), ``created_by`` (a user id; was the username
    in ``uploaded_by``), ``cashsale_total`` / ``nocashsale_total`` / ``expense_total`` (the
    stored aggregates), enum-typed ``status`` / ``publishing_status`` / ``discrepancy_type``,
    money as ``numeric(14,2)``, and the stamps have database defaults - so a row written
    here is indistinguishable from one Flask writes.

    What stays out: the per-method sales, the expense lines, the cash count and the Xero
    sync tables. If ``petty-cash-backend`` is ever extracted, this table goes with it and
    onboarding asks that service for the draft.
    """

    id = models.UUIDField(primary_key=True)
    entity_id = models.UUIDField()
    transaction_date = models.DateField()
    next_transaction_date = models.DateField(null=True, blank=True)
    status = PgEnumField("report_status", choices=ReportStatus.choices, default=ReportStatus.DRAFT)
    publishing_status = PgEnumField(
        "publish_status", choices=PublishStatus.choices, default=PublishStatus.UNPUBLISHED
    )
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    cash_addition = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    adjusted_opening_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    cashsale_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    nocashsale_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_sales = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # NULL means "not entered yet", which 0.0 could not express.
    expense_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    bank_deposit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    closing_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    safe_box_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    discrepancy_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    discrepancy_type = PgEnumField(
        "discrepancy_type", choices=DiscrepancyType.choices, default=DiscrepancyType.NONE
    )
    discrepancy_reason = models.CharField(max_length=300, null=True, blank=True)
    # Where the user resumes inside the report form, and which sections they finished.
    current_section = models.CharField(max_length=20, null=True, blank=True)
    completed_sections = models.JSONField(null=True, blank=True)
    xero_integrated = models.BooleanField(null=True, blank=True)
    # 'personal' | 'company': where the money ADDED to the float came from.
    cash_addition_type = models.CharField(max_length=20, null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "report"

    def __str__(self):
        return f"report {self.id} ({self.status}) for {self.entity_id}"


class Invitation(models.Model):
    """A pending team invite. Group B lists them; Group E creates and cancels them."""

    id = models.CharField(max_length=36, primary_key=True)
    entity_id = models.CharField(max_length=36)
    email = models.CharField(max_length=150)
    role = models.CharField(max_length=20)
    first_name = models.CharField(max_length=100, null=True, blank=True)
    last_name = models.CharField(max_length=100, null=True, blank=True)
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, default="pending")
    invited_by = models.CharField(max_length=36, null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    # NULL means a legacy row that never expires. Set at creation to
    # created_at + INVITATION_TTL_DAYS, in Hong Kong time.
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "invitations"

    def __str__(self):
        return f"invite {self.email} -> {self.entity_id} ({self.status})"


class SaleInfo(models.Model):
    """The GLOBAL catalogue of sales/payment methods (``sale_name`` is unique): Visa, Alipay,
    Foodpanda, Cash - and every name a company ever typed for itself. Which company uses
    which is ``EntitySaleSetting``. ``value_name`` is the sales form's field name
    (``visa_sales``); Cash is the row keyed ``cash_sales`` and its type is ``other``.

    Written here only when Step 5 meets a name nobody has used before.
    """

    CASH_VALUE_NAME = "cash_sales"

    id = models.UUIDField(primary_key=True)
    type = PgEnumField("sale_type", choices=SaleType.choices, default=SaleType.OTHER)
    sale_name = models.CharField(max_length=80, unique=True)
    value_name = models.CharField(max_length=80, null=True, blank=True)
    display_order = models.IntegerField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        managed = False
        db_table = "sale_info"

    def __str__(self):
        return f"{self.type}/{self.sale_name}"
