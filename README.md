# onboarding-backend

The onboarding wizard's API, extracted from Minty (the Flask "Module 1" monolith).
Django 5.2 + django-ninja, port **8001**.

The wizard frontend lives in a separate repo (`../onboarding`, Next.js, port 3001).

---

## The three rules this service is built on

Read these before changing anything. Each one has a failure behind it.

### 0. A SQLAlchemy `default=` is invisible to Django. Check before every write.

SQLAlchemy's `default=` is applied in Python on insert; only `server_default=` is a real DDL
default. Django sees just the second. So a column Flask "always fills" can have no database
default, and an insert here that omits it stores NULL.

`report.date` is one: a Django-written draft stored NULL, and Minty's Select Company page —
`datetime.now() - report.date`, no guard — took down the whole page with a TypeError.

Neither the suite nor the parity sweep catches this: SQLite builds its tables from the
mirrors, and parity only compares reads. `scripts/smoke_write.py` asserts it now.

**Before writing any table, grep the Flask model for `default=` without `server_`, and set
every one explicitly.**

### 1. Alembic owns the schema. This service ships no migrations.

Every model in `shared_models/models.py` is `managed = False`, and there is no
`migrations/` directory anywhere on purpose. The database is the same Postgres and the
same `pettycashv2` schema that Flask owns, and Alembic in Minty is the owner-of-record
for all DDL.

A new column means: **Alembic migration in Minty first, then hand-edit the model here.**
That hand-sync is a real cost, and it is the established convention — `billing-backend`
does the same, and `flask db migrate` is unusable in Minty anyway (it proposes recreating
all 42 tables).

`django.contrib.auth` and `contenttypes` are deliberately **not** in `INSTALLED_APPS`.
Installing them makes `migrate` want to create `auth_*` and `django_content_type` inside
a schema it does not own.

### 2. Whoever owns the external rail owns the write.

There are no `STRIPE_*` or `XERO_*` credentials here, and adding any is a bug:

- **Xero** rotates its refresh token on every use and invalidates the previous one. Two
  services refreshing means one POSTs a spent token, gets `400 invalid_grant`, and the
  customer's Xero connection stays broken until they manually reconnect.
- **Stripe** state lives in local tables with no webhook receiver, and Flask's
  `subscription/services/checkout.py` is ~3,200 lines of trial, proration and dunning
  logic treating them as source of truth. A second writer there charges twice.

So Flask keeps: cards, billing accounts, billing consent, trials, `finalize`, every Xero
token use, and **module enablement** (a module is granted by the subscription lifecycle,
not by a wizard step — `entity_function_map.is_enabled` is a projection of
`entity_module_subscription`).

**`core/minty_client.py` is the only module that calls Flask.** That is what makes the
rule checkable by reading one file. It forwards the caller's own bearer token, so the
proxy carries no privilege of its own and Flask applies exactly the checks it would have
applied to a direct call.

### 3. The error body key is `error`, not `detail`.

`billing-backend` answers `{"detail": ...}`. This service must not, because the wizard
reads `result.error` at some twenty call sites and resolves copy from
`data.error ?? data.message`. A `detail` body reaches the user as a blank toast.

This bites hardest on the auth path: ninja's built-in failure renders
`{"detail": "Unauthorized"}` *before* any endpoint is reached, so
`core/exceptions.py::on_unauthorized` exists specifically to override it.
`tests/test_auth.py::test_every_401_uses_the_error_key_not_detail` is the regression cover.

---

## Auth

Flask **mints** the JWT (60-minute HS256, `scope: "onboarding"`). This service only
**verifies** it. The two must share `SECRET_KEY` byte for byte — a mismatch is not a loud
boot failure, it is a 401 on every request, which the wizard reports as an expired session
and turns into a login loop.

Two deliberate departures from `billing-backend`:

| | billing-backend | here |
|---|---|---|
| Entity role at the door | Required | **Not required** — the first call *creates* the company the caller will have a role on. Membership is checked per endpoint via `core/permissions.py::entity_for_member`. |
| Entity id | `X-Entity-Id` header | **Query or body** — so the header is left out of `CORS_ALLOW_HEADERS` rather than sent and silently ignored. |

And one tightening: the `scope` claim **is** checked here. Flask sets it but never
verifies it, so today any token signed with the shared secret opens that surface. A token
that works against Flask can therefore be refused here. `core/auth.py::ACCEPT_ANY_SCOPE`
turns it off if that ever proves too strict — prefer fixing the caller.

---

## Migration groups

Flask's routes stay live throughout. The frontend's `lib/apiRoutes.js` decides which base
URL answers each path, so every group is independently revertible — moving a group is one
line, and reverting is the same line.

| Group | Endpoints | Where | Status |
|---|---|---|---|
| **A** Reference | `server-time`, `currencies`, `countries`, `plans` | Django | **Ported** |
| **B** Wizard state | `state`, `saved-step` | Django | **Ported** |
| **C** Entity | `create`, `PUT entity/<id>` | Django | **Ported** |
| **D1** Petty cash | `sales-methods`, `opening-balance` | Django | **Ported** |
| **E** Invites | `GET invite`, `invite/cancel` | Django | **Ported** |
| **D2** Xero & bills | `account-codes`, `contacts`, `contacts/create`, `bill-codes` | **Flask** | Proxy |
| **E** Invite send | `POST invite` | **Flask** | Proxy — the email links into a Flask route |
| **F** Modules | `modules` | **Flask** | Proxy — a module grant is a subscription write |
| **G** Money & Xero | `payment-method*`, `billing/*`, `finalize`, `xero/disconnect` | **Flask** | Proxy |

**13 of 33 method+path operations are implemented here; 20 are proxied.** That split is by
design, not by how far the work got — every proxied endpoint touches Stripe, a Xero token,
subscription state, or a Flask-owned URL. `tests/test_routes.py` holds the authoritative
list and fails if anything moves between the two sets without being declared.

Every Flask onboarding path is answered here, ported or proxied, so **the wizard points at
one base URL**. Groups D2, F and G unblock when `subscription-service` and `xero-service`
exist.

Paths match Flask **byte for byte** (`/api/onboarding/<name>`). That is what keeps the
cutover a base-URL swap with no path rewriting.

---

## Deliberate divergences from Flask

Two, both narrow, both with a reason. Everything else is a faithful port and the parity
sweep holds it to that — `EXPECTED_DIFFS` is empty, so every ported read is byte-identical.

1. **The `scope` claim is verified** (`core/auth.py`). Flask sets `scope: "onboarding"` and
   never checks it, so today any token signed with the shared secret opens that surface.
   A token that works against Flask can therefore be refused here.

2. **`saved_step` rejects floats and booleans** (`onboarding/services/steps.py`). Flask
   validates with a bare `int()`, so `2.5` truncates to step `2` and `true` becomes step
   `1` — both stored silently, and the user then resumes on a step they were never on.
   This is a write, so a refusal beats a different number. The wizard only ever sends
   integers, so nothing real changes. Digit strings stay accepted.

A third divergence was planned and withdrawn, recorded here so it is not re-attempted:
`GET /state` briefly returned a `steps` key, on the theory that the frontend duplicated an
ordering this service owned. It does not. `OnboardingApp.jsx` documents that `current_step`
here "is derived from a different ordering than the FE flow", deliberately does not use it
as a landing step, and names the bug that trusting it caused — *resume jumped straight to
Connect to Accounting*. The frontend's own table also carries short/tiny label variants and
module-conditional grouping this one never had. Two different questions, not two copies of
one answer.

Anything else that differs is a bug. Register a divergence in `EXPECTED_DIFFS` **with its
reason** or fix the code — never loosen the comparison.

---

## Running it

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env          # then set SECRET_KEY and the database to match Minty
.venv/Scripts/python manage.py check
.venv/Scripts/python manage.py runserver 8001
```

`manage.py migrate` is **not** part of setup — see rule 1.

## Testing

Two layers, and neither substitutes for the other.

```sh
.venv/Scripts/python -m pytest                  # unit: logic, auth, shapes (SQLite)
.venv/Scripts/python scripts/parity.py          # reads: vs live Flask on real Postgres
.venv/Scripts/python scripts/smoke_write.py     # writes: real Postgres, rolled back
```

**Three layers, and none of them is redundant.**

The suite runs on SQLite and builds its tables **from the models**, so a mirror that omits
a column simply has no such column in the test database: the insert succeeds and the suite
is green, while the same insert fails on Postgres. That is not hypothetical —
`entity_function_map.created_at` and `updated_at` are NOT NULL with no database default,
they were missing from the mirror, and only a real write found it.

`scripts/parity.py` covers the reads: it calls both services with the same token against
the same database, sweeps every entity rather than sampling one, and compares status and
body exactly.

`scripts/smoke_write.py` covers the writes, inside a transaction that always rolls back.
It is the only layer that can catch a **wrong default**, which is worse than a missing
column: `entity_function_map.is_enabled` defaults to `true` in the database, so a row
inserted without naming it grants the module — and a SQLite test, whose default comes from
the model, would report `False` and pass.

**A group is not done because its tests are green — it is done when parity reports MATCH
(or a registered EXPECTED) for every read in it and the write smoke test is clean.** That is what makes deleting the Flask route safe.

Retiring a Flask route also means editing the `csrf.exempt` block in Minty's
`services/app_runtime/legacy/bootstrap.py` and the two tests that pin it
(`test_csrf_exemptions.py`, `test_onboarding_csrf_exempt.py`).
