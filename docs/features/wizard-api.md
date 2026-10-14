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
the registries Step 1 needs (`currency_info`, `country_info`), read from the database. Each
currency carries `decimal_places` (Step 5 types the opening balance in it; JPY is 0) and each
country its own `currency_id` (or null), which Step 1 preselects when the country is picked.
`GET /plans` — the module price list for Step 2's summary, **from `billing_plan`, not
Stripe** (`services/plans.py`).

## Resume: `/state` and `/saved-step` (`services/state.py`, `steps.py`)

**The `entities` row is the source of truth, not the browser.** `GET /state?entity_id=`
rebuilds the whole picture from the database — modules, Xero, the Step 6 flag and the
opening balance from ONE `entity_setup_summary` row (a Postgres view, so the tests that read
`/state` run in the Postgres pass only; the live Xero revoke check runs first) — entity, name, country, currency, phone,
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
(`created: false`). `PUT /entity/{id}` edits an in-progress one. Both take `timezone` -
the browser's IANA zone, sent with no field on screen; unknown or longer than the 30-character
column is 400 (`resolve.timezone`). Create derives the currency from the country when none is
sent (as edit always has) and sets the creator's `user_entity.joined_at`. Country and currency
are **resolved against the registries** (`resolve.country_code` accepts alpha-2, alpha-3
or a unique name; `currency_id` only queries once the value is a uuid) and never stored
unvalidated — they are foreign keys; phone and email are checked the way the wizard does.
The phone (`resolve.contact_phone`, since 2026-10-12) is **E.164**: `+<dial code><number>`,
spaces, dashes, dots and brackets dropped, the domestic trunk 0 dropped (except +39/+378), and
the code must be a `country_info.phone_code` (so a database without
`Minty/docs/schema/seed_country_phone_codes.sql` refuses every number). Only Hong Kong's length
is exact: 8 digits for +852, the one market Minty sells in (`PHONE_LENGTHS`, the wizard's table,
so change both together; add a code when its market opens). Every other code: at least 4
national digits and at most 15 in all. Stored as
`+85291234567`. Any miss is a 400 `"Please check the phone number and its country code."`. A
digits-only value (the pre-2026-10-12 shape) is refused. Stored rows are not rewritten: the
wizard splits an old one under the company's Country when Step 1 opens. `GET /countries`
carries `phone_code` ("+852", "" when the row has none).
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
- `POST /opening-balance` — the starting cash and the date reporting starts, saved as
  `entity_pettycash_settings.opening_balance` / `start_date` (the row is created if Step 6
  has not made it yet; `services/opening_balance.py`). No report is written: Minty's first
  report opens at the amount, defaults to the date and may not start before it. Refused
  with 409 once any report is posted; a future date, a negative or non-numeric amount, an
  amount finer than the currency's `decimal_places`, or a company with no currency yet is
  400. Flask's fallback (`save_opening_balance`) does the same.
- `GET/POST /account-codes`, `POST /contacts`, `POST /contacts/create` (a new Xero
  contact), `GET/POST /bill-codes` — the expense accounts, the petty-cash contact mapping
  and the bills' account codes, over the rows Minty synced.

## Invitations (`services/invites.py`)

`GET /invite` lists pending invitations (`USER_VIEW_ALL`) with `expires_at` and `expired`
(Step 3 shows "Expires in N days" / "Expired"), `POST /invite/cancel` marks one `revoked`
(never deleted). **Sending stays in Flask** (`POST /invite` and `POST /invite/resend` proxy -
resend gives a fresh link and expiry, with Flask's cooldown as a 429 `{error, retry_after}`)
— mail has no single-writer constraint, but the templates and the accept flow live there.

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
`entity_for_member` (400/403/404). First the setup rule (`setup_gaps`, from
`entity_setup_summary`, only when a module is on): Petty Cash needs Xero plus all six accounts
and three contacts; Payment Request needs Xero plus at least one ticked bill account code, and
a default code (if set) that is ticked. Anything missing is a 409 `{"error", "missing": [{"step",
"message"}]}` and the company stays `onboarding` - All Set lists each with a way back to its
step. Then a company still `onboarding` flips to `connected` (a Xero
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
