# The wizard's API — `/api/onboarding/*`

Everything the onboarding wizard (`../minty-onboarding-web`) calls, served here byte-for-byte on the
same paths Minty's Flask still answers (`Minty/blueprints/entity/routes/create.py`; not
`payment-method`, `billing/*` or `finalize` since 2026-10-06) — the
wizard's `lib/apiRoutes.ts` decides which service gets each path. Routers:
`onboarding/api_reference.py`, `api_state.py`, `api_entity.py`, `api_pettycash.py`,
`api_invites.py`, `api_modules.py`, `api_billing.py`, `api_me.py`; services under
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
rebuilds the whole picture from the database — modules, Xero and the opening balance from ONE
`entity_setup_summary` row (a Postgres view, so the tests that read `/state` run in the
Postgres pass only; the live Xero revoke check runs first), and the Step 6 flag from
`entity_pettycash_settings.pettycash_account_id` (the view has no such column; c5a05 added
one and was withdrawn 2026-10-14) — entity, name, country, currency, phone,
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
(`created: false`). `PUT /entity/{id}` edits an in-progress one. Both set
`entities.timezone` from the country (`minty_shared.time.country_timezone`, the tracker's map;
owner, 2026-11-04) - a sent `timezone` is ignored, and a country the map lacks keeps the stored
zone with a warning in the log. Create derives the currency from the country when none is
sent (as edit always has) and sets the creator's `user_entity.joined_at`. **The creator is the
company's subscriber** (owner, 2026-11-13; minty-db v0.9 `entities.subscriber_user_id`). Only
they may activate or change its subscription until they hand it over, and minty-subscription-api
enforces that. A resumed company with no subscriber gets its creator, never over one already set. **Create requires a
country and a currency** (owner, 2026-10-19: Step 1 no longer pre-fills Hong Kong / HKD): no
country is a 400 `"Choose the company's country."`, and no currency sent or derivable from the
country's registry row is a 400 `"Choose the company's currency."` — both before the company is
created. A name over 100 characters (after the trim) is a 400 `"Entity name must be 100
characters or fewer."` on create as on rename (`NAME_MAX`, 2026-10-26; create had no limit
before). **A name is unique per person** (owner, 2026-11-03; it was unique across
every customer): create and rename are 409 `"You already have a company called “X”. Choose
another name."` only when the acting user already belongs to a company of that name
(`entity_create.user_has_company_named`); another customer's company of the same name is no
conflict. The wizard keys on the 409 alone and writes its own sentence. `PUT /entity/{id}` does not require a country or currency (a partial edit stays valid). Country and currency
are **resolved against the registries** (`resolve.country_code` accepts alpha-2, alpha-3
or a unique name; `currency_id` only queries once the value is a uuid) and never stored
unvalidated — they are foreign keys; phone and email are checked the way the wizard does.
The phone (`resolve.contact_phone`, since 2026-10-12) is **E.164**: `+<dial code><number>`,
spaces, dashes, dots and brackets dropped, the domestic trunk 0 dropped (except +39/+378), and
the code must be a `country_info.phone_code` (so a database without
the dial codes in `Minty/scripts/schema_migration/pettycashv3_data_post_build.sql` refuses every number). Only Hong Kong's length
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
  rows are shared and get-or-created; permission `SALES_METHOD_CREATE`). Free lists with no
  account link (owner, 2026-11-11: the one-day link to step 4's sales categories is gone, and
  the column, an unreleased minty-db v0.6, was dropped again). Which lists follow the business type
  (`sales_methods.groups`): `GET` (and `/state`'s `sales_methods`) carries `groups` -
  `non_cash` for a retail shop, a service or a general business (step 5 shows one Non-cash list,
  posted as `electronic` with `delivery: []`), `electronic_delivery` for Restaurant & Food and a
  company onboarding never asked. Petty Cash groups the same way (minty-petty-cash-api's
  `sales_groups`).
- `POST /opening-balance` — the starting cash and the date reporting starts, saved as
  `entity_pettycash_settings.opening_balance` / `start_date` (the row is created if Step 6
  has not made it yet; `services/opening_balance.py`). No report is written: Minty's first
  report opens at the amount, defaults to the date and may not start before it. Refused
  with 409 once any report is posted; a future date, a negative or non-numeric amount, an
  amount finer than the currency's `decimal_places`, or a company with no currency yet is
  400. A **blank amount is refused, not saved as 0** (2026-10-26): no amount key, `null`, `""`
  or whitespace is 400 `"The opening balance is still empty. Add the amount to continue."`
  (the wizard skips the call while its field is blank). Flask's fallback (`save_opening_balance`) does the same.
- `GET/POST /account-codes`, `POST /contacts`, `POST /contacts/create` (a new Xero
  contact), `GET/POST /bill-codes` — the expense accounts, the petty-cash contact mapping
  and the bills' account codes (`services/xero_lists.py`; ported from Flask 2026-11-06, Phase
  4 of the Flask retirement - proxied until then). Flask's answers, statuses and sentences;
  `COA_VIEW` to read, `COA_UPDATE` to save, `CONTACT_CREATE` for a new contact (403
  `{"error": "Access denied"}`); no Xero yet or no working token is 409
  `{error, connected: false}`. The token comes from accounts-api and Xero is called through
  `core/xero_client.py` - this service never refreshes one.
  - `GET /account-codes` asks Xero live (accounts and every page of contacts), as Flask did,
    because only Xero's answer marks the system accounts it leaves out (the Director list, the
    codes). Xero not answering is a 502 with a sentence (Flask showed empty lists).
  - `POST /account-codes` asks accounts-api's one sync first (`POST /api/internal/xero/sync`),
    then resolves each chosen Xero id to its stored row (a choice still missing is 409 naming
    it; Flask kept the old value silently); a key absent keeps its column, an empty one clears
    it; the discrepancy bank mirrors the petty cash bank; the ticked codes are exactly the
    posted ones, at least one when the company has any. With no `expense_codes` key (the Xero
    path's step 6, 2026-11-11: X3 chose them) only the mapping is saved, the ticks stay as
    they are and the answer's `expense_codes` is null.
  - `POST /contacts` resolves from the stored contacts, asking the sync once only when one is
    missing. A company banking all its sales (step 4) keeps no cash-sale contact: it is cleared
    whatever is posted (2026-11-11). `POST /contacts/create` makes a supplier in Xero and stores it
    (`category = expense_contact`); Xero refusing is 502.
  - **`/bill-codes` follows Accounting Setup (owner, 2026-11-11):** the list is step 4's expense
    accounts (`accounting.payment_accounts`) - a Minty company's own (4.4), a Xero company's ticked
    in X3 (or by the old wizard) - with no Xero call. Each is ticked by its old
    `entity_bill_account_xero` row when one exists (what minty-payment-request-api reads), else by
    Payment Request's `account_module` row; `default_all` is true only when none has either.
    `POST` ticks exactly the posted codes among them (at least one: "Pick at least one account
    code.", a default among them: "The default account code must be one of the ticked codes.") and
    writes **both** tables - `account_module` PAYMENT_REQUEST (`is_active`, one `is_default`, made
    for a Xero account that has none) and the old rows (made when missing, and turned off for any
    account step 4 doesn't list). A company with no listed account is 409 "Add an expense account
    in Accounting Setup first."

## Accounting Setup - step 4 with Xero optional (`api_accounting.py`, `services/accounting.py`)

New 2026-11-10 (Figma `LGkVZqcyxSIh6pp5ihDjXw` node 348:1007; the design is Minty's
`docs/modernisation/xero_optional_database.md`; the columns are minty-db v0.5's EXPAND). Step 4
asks how Minty keeps the books; on its own, the company answers two questions and picks its own
accounts, with no Xero at all. Every route passes `entity_for_member` first, then the role check
(`COA_VIEW` to read, `COA_CREATE`/`COA_UPDATE`/`COA_DELETE` to change; refused is 403
`{"error": "Access denied"}`). Refusals are `{"error", "fields"?}` with Part 11 sentences: 400 for a
field, 409 for a code or name another active account holds.

| Route | Screen | What |
|---|---|---|
| `GET /accounting?entity_id=` | all of step 4 | the answers (`mode`, `business_type`, `uses_sales_cash`, `business_answered`), `accounts.sales`/`accounts.expense` (hidden system accounts left out; each row's `auto_filled` = an Auto Fill row nothing uses yet, what Revert would take out - 2026-11-11: a kept Cash Over / Short made Revert look as if it filled again), the five `slots` with their `options` (right types only), `slots_ready`, `next_code` per type, `types` |
| `POST /accounting/mode {mode}` | 4.1 | `minty` or `xero` -> `entities.setup_mode`. `minty` is refused (409) while a Xero org is linked. `xero` is free during onboarding (owner, 2026-11-11, no question asked): it removes every account Minty made for the company (`source` template or user, not the hidden system rows - the business type's set, Auto Fill's and the person's own; `_clear_own_chart`) and the daily-closing choices that pointed at them, in one transaction. Refused (409, nothing changed) only when a report expense or bill line already uses one: _This company's reports or bills already use its accounts in Minty, so it keeps using Minty on its own for now. Connect Xero from Settings once setup is finished._ (the hub's matching) |
| `POST /accounting/business {business_type, uses_sales_cash}` | 4.2 | `fnb`/`retail`/`service`/`general`, or null = "None of these"; the cash question is required for every company and "Not sure" arrives as `true`. On the Minty path the template's shared system rows (Petty Cash, Bank, Amount Due to Director) are added then, hidden, and fill their daily-closing accounts. Since 2026-11-11 (owner: no sales screen) the set's ticked sales accounts are added too (`_sales_from_set`), so Cash Sales fills its daily-closing account; changing the answer removes the old set's unused sales rows (and releases its cash-sale slot) before adding the new set's. The company's Cash sales method (`entity_sale_setting` for `sale_info.value_name = 'cash_sales'`) follows the answer: `false` ("We bank all sales") turns it off, `true` on (linked if missing); such a company's `GET /accounting` has no `cash_sale` slot (owner's brief, 2026-11-10) |
| `GET /accounting/autofill?kind=` / `POST {kind, template_ids}` / `POST /accounting/autofill/revert {kind}` | 4.3, 4.4 | the company's template set for `sales` or `expense` (its business type, or General for "None of these"; its cash answer; its country or every country), ticked where the template says; adding skips a row already there and refuses one whose code or name is taken; each added account shows in the modules its row ticks (`pettycash_default` / `payment_request_default` -> `account_module` and the old `entity_account_xero` / `entity_bill_account_xero` rows, inactive where unticked), and a row with a `pettycash_role` (e.g. Cash Sales) fills that daily-closing account when it is still empty (2026-11-10, the accounting team's sheet); an account the person adds is in both modules; Revert removes the Auto Fill rows nothing uses. The templates are Minty's `docs/schema/seed_account_templates.sql` |
| `POST /accounting/accounts {code, name, type, description?, role?}` / `PATCH /accounting/accounts/{id}` / `DELETE /accounting/accounts/{id}` | 4.3b, 4.4a, 4.5a | one account: code (letters, numbers, dots, dashes; up to 10; a bank account may have none), name (up to 80 until CONTRACT widens it), type; `role` also chooses it for that daily-closing account. An account from Xero is changed in Xero (409). Removing deletes an unused account and archives a used one; a system account can't be removed |
| `POST /accounting/xero-accounts {kind, account_ids}` | X3 (Xero path, 2026-11-10; X2's sales screen removed 2026-11-11, the route still takes `sales`) | Exactly these of the synced Xero `sales`/`expense` accounts are Minty's: Petty Cash's `account_module` rows (`is_active`), and for expenses `entity_account_xero.is_active` too (`xero_lists._tick_codes`, the apps' old reader). 400 for none ticked ("Tick at least one sales account to continue."), a row of the wrong kind, a Minty-made or another company's row; 409 when not connected (a kept org after a disconnect is not). Answers the whole step (`GET /accounting`), whose rows now carry `used` |
| `POST /accounting/xero-refresh` | Refresh from Xero (X2-X4) | accounts-api's one sync (`core/xero_client.sync`), then the whole step again; 502 _Xero didn't send your accounts just now. Try Refresh again in a moment._; 409 when not connected |
| `POST /accounting/slots {role, account_id \| null}` | (4.5, removed from the wizard 2026-11-11; kept for the route's callers) | `petty_cash`, `bank`, `cash_sale`, `discrepancy`, `director` -> `entity_pettycash_settings`; an account of the wrong type is refused, and petty cash and the bank can't be one account. Petty cash also fills `discrepancy_bank_account_id` (as the old step 6 did; Petty Cash and finalize read it). Missing ones only warn: Petty Cash asks again before the first closing |

Expense accounts show under two headings from their type (`group`: `direct_costs` =
`cost_of_sales`, "Cost of what you sell"; `expense`/`overheads` = `running_costs`), never a stored
group (decision 19). **Until each module's own release moves it, an account made here is written
for the old readers too:** `account_info`'s old `type` (Xero's spelling), `xero_code` and `status`
beside Minty's `code`/`account_type`/`record_status`/`source`, and an expense account also gets
Petty Cash's tick (`entity_account_xero`), a Payment Request bill code (`entity_bill_account_xero`
with no Xero id, like a hand-added code) and its `account_module` rows.

The rest of the wizard follows `setup_mode`: `/state` carries `accounting` (the answers) and
moves a Minty-only company past step 4 once the business questions are answered (its accounts
are chosen inside step 4); finalize asks it for neither Xero nor the Xero-only accounts and
contacts, only the business answers, and it goes live as `disconnected` (the only live value
before CONTRACT) while the trigger keeps `xero_status = 'not_connected'`; step 8's bill codes are
its own expense accounts, with no Xero call. With a mode chosen (a Xero org sets `xero`), `/state`
counts Petty Cash's step (5) done once its opening balance is saved, since step 4 chose the
accounts (2026-11-11). Step 5's sales methods refuse a method named Cash (400 "Cash is set by your
answer in Accounting Setup."): Cash follows step 4's cash answer only.

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

## The sidebar's My Profile sheet (`api_me.py`, proxied since 2026-11-03)

The wizard draws `@minty/shared`'s sidebar, whose My Profile sheet reads and saves the
person's own profile and lists their subscriptions, and its cat button sends "Talk to Minty" messages. Neither Flask's nor the subscription API's
CORS admits the onboarding origin, so the wizard reaches them through four pass-through
proxies here (the caller's bearer forwarded, the upstream's status and body relayed unchanged;
nothing is validated here):

- `GET /me/profile[?entity=]` → Flask `GET /api/me/profile` (`entity` forwarded only when sent;
  Flask answers 403 for a company the person may not see).
- `PATCH /me/profile[?entity=]` → Flask `PATCH /api/me/profile`, the JSON body
  (`{first_name?, last_name?, email?}`) forwarded as is; Flask validates (400/422 with the
  sentence to show).
- `GET /me/subscriptions[?page=&per_page=]` → minty-subscription-api `GET /api/me/subscriptions`
  (paging forwarded only when sent, for that service to validate).
- `POST /me/contact` → Flask `POST /api/me/contact`, the "Talk to Minty" chat's message
  (`{topic, message, app, entity?}`) forwarded as is; Flask takes the sender from the token,
  validates and mails it (403/422/429/502 with the sentence to show).

They are not Flask onboarding paths. Both upstreams accept the onboarding-scoped token (Flask's
`hub_api.guard` and the subscription API's `SelfBearerAuth` check the signature and the person,
not `scope`).

## Finalize (`api_billing.py`, implemented here since 2026-10-06)

`POST /finalize {entity_id}` — called on arrival at All Set. Membership via
`entity_for_member` (400/403/404). First the setup rule (`setup_gaps`, from
`entity_setup_summary`, only when a module is on): a company keeping its books in Minty on its
own (Accounting Setup, above) needs only its business answers; otherwise Petty Cash needs Xero
plus daily closing's accounts (four for a company banking all its sales, else five; the
discrepancy bank follows petty cash) -> step 6, Account Code (2026-11-11: step 4 no longer maps
them), and the director, discrepancy and (with
cash sales only) cash-sale contacts -> step 7, read from `entity_pettycash_settings` (2026-11-11); Payment Request needs Xero plus at least one ticked bill account code, and
a default code (if set) that is ticked - that one read from `entity_bill_account_xero`
(`EntityBillAccountXero`), since the view has no column for it. Anything missing is a 409 `{"error", "missing": [{"step",
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


## Settings' accounts and contacts (`onboarding/internal.py`, `services/chart.py`; owner 2026-11-12)

The ONE implementation of a company's accounts and contacts add / edit / delete - the wizard's
account steps and the hub's Settings share it (`accounting._create`, `_apply_update`, `_retire`:
the checks, the old readers' rows, the module rows, the daily-closing slots). The hub reaches it
through accounts-api (`/api/me/company/accounts|contacts`), which calls
`/api/internal/chart/{accounts,contacts}[/<id>]` with a service assertion (scope `company-chart`,
claims `entity_id` and `user_id`; anything else 401). The person's rights are checked again here.

- **Who owns what.** Every row is the company's own copy, so any is edited or deleted (owner,
  2026-11-12): `source` `user`, `xero`, and `template` (its copies of Minty's starter chart and
  template contacts; Minty's `account_template` is never touched). Only the hidden system rows stay
  out. The read marks each row `own`, `source`, `in_xero`, `used`; it also sends the types and each
  type's next free code (`minty_shared.accounts`).
- **Minty's accounts** (owner, 2026-11-12): the read's `minty_accounts` are the starter chart
  (`account_template`) for the company's country, every business type's, one per name, less the
  hidden system rows and any the company has; `POST {template_id}` adds one as Minty's copy
  (`accounting._from_template`: its module and daily-closing defaults, as Auto Fill) - in Xero
  first when live; a code already taken is refused (409), one no longer offered is 404.
- **With or without Xero.** A company live on Xero (`xero_connected` + a token) gets each change in
  Xero first - `PUT /Accounts`, `POST /Accounts/{id}` (an edit, or `Status: ARCHIVED`), `POST
  /Contacts` (a new one, a rename or `ContactStatus: ARCHIVED`); Xero's refusal is a sentence
  (422, `core/xero_client.write`), unanswered 502, and nothing changes in Minty. A live company with
  no token: 409, reconnect. Not live: the company's own Minty rows; a row already linked to Xero
  waits for a reconnect (409).
- **Delete is complete**: deleted when unused, else archived (kept for its records, off every list
  and picker, its module rows off).
- **No duplicate contacts.** Names are unique per company among active ones (case and inner spaces
  ignored; 409 naming the existing one). A live company's new contact links to the stored Xero
  contact of that name before making one; a Minty-made contact met while live goes into Xero
  then (`find_or_make_contact`, also behind the wizard's `/contacts/create`).
- **Deleted, then added again: brought back, never a twin** (owner, 2026-11-12). An account added
  with the code, name and type of one the company deleted - or a contact with the name of one - is
  that one: Minty's archived row restored (its name kept; modules and old-reader rows on again), or,
  its row gone, the archived Xero copy linked to a new row; set ACTIVE in Xero when it is there
  ("<name> is back."). A delete marks Minty's copy (`xero_account` / `xero_contact`) archived at
  once, so nothing links to it meanwhile. Offline, a row still linked to Xero is refused: "<name>
  was archived in Xero. Reconnect Xero to bring it back."
- Tests: `tests/test_chart.py`.

## Tests

`tests/test_state.py`, `test_entity.py`, `test_pettycash.py`, `test_invites.py`,
`test_reference.py`, `test_accounting.py` (Accounting Setup), `test_routes.py` (every path is
registered; 30 ported, 17 proxied, 47 in all, 2026-11-10), `test_me_proxies.py` (the My Profile proxies), `test_finalize.py`, `test_char_schema.py`
and `test_schema_name.py` (the schema), `test_settings_guard.py`; 399 passed + 1 skipped on
2026-10-06 (SQLite and Postgres) against Minty's `01_schema_rebased.sql` (`MINTY_REPO`). End to end:
`minty-onboarding-web/e2e` (`stack`, `resume`, `xero`, `walk`).

**Connecting Xero later (Settings Phase 2, 2026-11-11).** `accounting.minty_on_its_own` is
`setup_mode = 'minty'` alone - an organisation recorded while minty-accounts-api matches the
company's accounts leaves it Minty's until matching's Finish sets `'xero'`. Finalize never marks such
a company `connected` (only matching does, once its accounts and suppliers are in Xero).
