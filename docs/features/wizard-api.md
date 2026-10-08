# The wizard's API — `/api/onboarding/*`

Everything the onboarding wizard (`../minty-onboarding-web`) calls, served here byte-for-byte on the
same paths Minty's Flask still answers (`Minty/blueprints/entity/routes/create.py`; not
`payment-method`, `billing/*` or `finalize` since 2026-10-06) — the
wizard's `lib/apiRoutes.ts` decides which service gets each path. Routers:
`onboarding/api_reference.py`, `api_state.py`, `api_entity.py`, `api_pettycash.py`,
`api_invites.py`, `api_modules.py`, `api_billing.py`; services under
`onboarding/services/`. The migration groups (A–G) and the deliberate divergences from
Flask are in the repo `README.md`.

## Reference (public)

`GET /server-time` — "today" in Hong Kong, server-authoritative, so a date the wizard
defaults to never comes from the browser's clock. `GET /currencies`, `GET /countries` —
the registries Step 1 needs (`currency_info`, `country_info`), read from the database.
`GET /plans` — the module price list for Step 2's summary, **from `billing_plan`, not
Stripe** (`services/plans.py`).

## Resume: `/state` and `/saved-step` (`services/state.py`, `steps.py`)

**The `entities` row is the source of truth, not the browser.** `GET /state?entity_id=`
rebuilds the whole picture from the database — entity, name, country, currency, phone,
email, status, `modules`, `xero` (connected? tenant), `sales_methods`, `opening_balance`,
`invites` (best-effort: a cashier who may not list invitations still gets a state),
`saved_step`, `current_step`, `max_reached` — so a cold resume
(new browser, cleared storage) lands correctly.

Two numbers, two meanings, one 1–9 scale:
- `saved_step` — the frontend's step id, stored verbatim by `POST /saved-step` ("Save and
  Exit"); `is_valid_step` refuses anything but an integer 1–9 (stricter than Flask, which
  truncated `2.5`).
- `current_step` — `derive_current_step`: the furthest step the **saved data** justifies
  (live company → All Set; no modules → Modules; no Xero → Accounting; Petty Cash chosen
  but account codes missing → Sales; Bills chosen → Bills; else Invite). The wizard uses
  it only to raise the ceiling of unlocked steps — its own landing order differs on
  purpose, and the module docstring records the bug that trusting it caused.

Step ids: 1 Basic, 2 Modules, 3 Invite, 4 Accounting, 5 Sales, 6 Account code, 7 Others,
8 Bills, 9 All Set.

## The company (`services/entity_create.py`, `resolve.py`)

`POST /create` — creates the company owned by the caller with its default settings (sales
methods: **Cash only** — no electronic or delivery method is seeded, so the wizard's lists start
empty until its Auto Fill);
idempotent on (member, name, `status = onboarding`) so a retry resumes the abandoned row
(`created: false`). `PUT /entity/{id}` edits an in-progress one. Country and currency
are **resolved against the registries** (`resolve.country_code` accepts alpha-2, alpha-3
or a unique name; `currency_id` only queries once the value is a uuid) and never stored
unvalidated — they are foreign keys; phone and email are checked the way the wizard does.
The business email (`resolve.business_email`, `POST /create` and `PUT /entity/{id}`) is
English only since 2026-10-01: `resolve.EMAIL_RE` = printable ASCII minus "@", one "@", a dot
in the domain (still shallow: `a+b@sub.domain.museum` passes). A non-ASCII address is a 400
`"Email can only contain English letters, numbers and symbols."`; any other miss is a 400
`"Please enter a valid business email."` (a dotless domain is one since that date). Stored rows
are not rewritten. Invitation emails are not checked here: `POST /invite` forwards to Flask,
which refuses non-ASCII itself, and its 400 reaches the wizard unchanged (`minty_client.proxy`).

The optional business email (`entities.business_email`) is not just stored (2026-09-30): the
billing engine (`minty-subscription-api`, `notify.address_for`; Minty's copy deleted 2026-10-06) sends the company's trial
ending warning there, and a billing account with no billing email of its own mails its payment
emails - and prints its invoices' Bill to - to the business email when every company on the
account shares it. Blank means the payer's own address is used instead.

## Petty-cash setup (`api_pettycash.py`)

- `GET/POST /sales-methods` — the electronic and delivery methods, **reconciled** to the
  submitted name lists, never delete-and-reinsert (`services/sales_methods.py`; catalogue
  rows are shared and get-or-created; permission `SALES_METHOD_CREATE`).
- `POST /opening-balance` — the starting cash, written as `opening_balance` with
  `cash_addition = 0` on the entity's onboarding draft (`services/opening_balance.py` —
  every branch there is load-bearing; read it before touching it).
- `GET/POST /account-codes`, `POST /contacts`, `POST /contacts/create` (a new Xero
  contact), `GET/POST /bill-codes` — the expense accounts, the petty-cash contact mapping
  and the bills' account codes, over the rows Minty synced.

## Invitations (`services/invites.py`)

`GET /invite` lists pending invitations (`USER_VIEW_ALL`), `POST /invite/cancel` marks
one `revoked` (never deleted). **Sending stays in Flask** (`POST /invite` proxies) — mail
has no single-writer constraint, but the templates and the accept flow live there.

## Proxied to Flask (`api_modules.py`, `api_billing.py`, `core/minty_client.py`)

`POST /modules` (the map is a projection of subscription state — Flask owns it) and
`POST /xero/disconnect` are forwarded as the caller with Flask's own status code.

## Proxied to minty-subscription-api (`api_billing.py`, `core/subscription_client.py`)

Since 2026-10-06 `GET /payment-method` and the seven `billing/*` routes (`payment-methods`
GET, `payment-methods/setup-intent`, `payment-methods/confirm`, `payment-methods/default`,
`accounts` GET/POST, `authorize` — Stripe customers, SetupIntents, cards, consent; every card
is confirmed in-app onto a billing account; the hosted-Checkout `payment-method/setup` and
`/complete` proxies were removed 2026-10-01) go to `SUBSCRIPTION_API_URL` on the same paths.
`core/subscription_client.py` is the only module that calls that service; it reuses
`minty_client`'s transport (`base=`): the caller's bearer forwarded verbatim, the same timeout,
the same 502/504 answers.

## Finalize (`api_billing.py`, implemented here since 2026-10-06)

`POST /finalize {entity_id}` — called on arrival at All Set. Membership via
`entity_for_member` (400/403/404). A company still `onboarding` flips to `connected` (a Xero
org is linked) or `disconnected`; then **every** finalize calls the subscription API's
`POST /api/onboarding/trials/start {entity_id}`, so a retry reaches it. A non-200 answer fails
finalize with that service's own status and `error` sentence (the company stays live; the
wizard's Try again redoes the trial start); success is `{"status": "success", "trial_end":
iso | null}`. Both halves are idempotent. Flask's `/api/onboarding/finalize` no longer exists.

The trials it starts have **no subscriber** (2026-10-08): `trials/start` establishes no
payer, because a free trial commits nobody. The wizard's door onto the billing relationship
is the proxied `POST /billing/authorize` on step 2, and it may be skipped - nothing here
changes, but a company that skipped it goes live with trials that expire rather than convert
until an admin confirms billing in minty-web.

## Tests

`tests/test_state.py`, `test_entity.py`, `test_pettycash.py`, `test_invites.py`,
`test_reference.py`, `test_routes.py` (every path is registered; 14 ported, 17 proxied, 31 in
all), `test_finalize.py`, `test_char_schema.py`
and `test_schema_name.py` (the schema), `test_settings_guard.py`; 399 passed + 1 skipped on
2026-10-06 (SQLite and Postgres) against Minty's `01_schema_rebased.sql` (`MINTY_REPO`). End to end:
`minty-onboarding-web/e2e` (`stack`, `resume`, `xero`, `walk`).
