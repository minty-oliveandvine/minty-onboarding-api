# Manual QA checklist — onboarding API

A manual walkthrough checklist for `/api/onboarding/*`, to run alongside the automated
suites (`## Tests` in [wizard-api.md](wizard-api.md) and [authentication.md](authentication.md)).
This is not a replacement for those — it exists for exercising the service by hand (Postman,
curl, or through the wizard at `minty-onboarding-web`) before a release, and for checking the
sharp edges the docs call out explicitly. Tick each box against a disposable test entity —
never a real one (see "Never let a test navigate to step 9" below).

## Auth (`OnboardingBearerAuth`, [authentication.md](authentication.md))

- [ ] No `Authorization` header on a non-public endpoint → 401.
- [ ] Malformed token → 401.
- [ ] Token signed with the wrong secret → 401.
- [ ] Expired token → 401 (the wizard's "session expired" message comes from here).
- [ ] Token just inside the 60 s clock-skew leeway → accepted.
- [ ] Token with `scope` ≠ `"onboarding"` → 401.
- [ ] Token for a `user_id` that doesn't exist → 401.
- [ ] `server-time`, `currencies`, `countries` answer with **no** token.
- [ ] A valid token with no role on the target entity can still call `POST /create` (no
      door-level role check — the first call creates the role).
- [ ] A valid token naming an entity the caller has no `user_entity` row for → 403 on any
      entity-scoped endpoint.
- [ ] Entity id passed as a header (`X-Entity-Id`) is ignored — only query string / JSON body
      is read.

## Reference data (public)

- [ ] `GET /server-time` returns Hong Kong "today", independent of the client's clock/timezone.
- [ ] `GET /currencies`, `GET /countries` return the full registries used by Step 1.
- [ ] `GET /plans` reflects `billing_plan`, not whatever Stripe currently has configured.

## Resume: `/state` and `/saved-step`

- [ ] `GET /state?entity_id=` on a fresh browser/cleared storage reproduces the exact step the
      entity was last saved on.
- [ ] `POST /saved-step` with an integer 1–9 succeeds; `2.5`, `0`, `10`, and non-numeric values
      are refused (stricter than Flask, which truncated decimals).
- [ ] `current_step` (derived) rises appropriately as data is added — no modules → Modules;
      Petty Cash chosen but account codes missing → Sales; Bills chosen → Bills; nothing
      outstanding → Invite; live company → All Set.
- [ ] A cashier who cannot list invitations still receives a `state` response (best-effort
      `invites`), not an error.

## Creating / editing the company (`POST /create`, `PUT /entity/{id}`)

- [ ] `POST /create` twice with the same member + name while still `onboarding` resumes the
      same row (`created: false`) rather than duplicating it.
- [ ] Default sales methods on a new company are empty (no seeded Cash/electronic/delivery
      rows) until Auto Fill is used.
- [ ] Country accepted as alpha-2, alpha-3, or a unique name; an unrecognised country string
      is refused, not silently stored.
- [ ] Currency accepted only once resolved to a real `currency_id`.
- [ ] Business email with non-ASCII characters → 400 `"Email can only contain English letters,
      numbers and symbols."`
- [ ] Business email with no dot in the domain, or otherwise malformed → 400 `"Please enter a
      valid business email."`
- [ ] A valid business email is stored and **not** rewritten by a later edit that omits it.
- [ ] `PUT /entity/{id}` on an entity that is no longer `onboarding` is refused (can't edit a
      live company through this route).

## Petty-cash setup

- [ ] `GET/POST /sales-methods` reconciles against the submitted name list — rows already
      present are reused, not deleted-and-reinserted (check ids are stable across two saves
      with overlapping names).
- [ ] `POST /opening-balance` writes `opening_balance` with `cash_addition = 0` on the
      onboarding draft — not on any already-finalized report.
- [ ] `GET/POST /account-codes`, `POST /contacts`, `POST /contacts/create`, `GET/POST
      /bill-codes` all operate over rows Minty already synced from Xero — none of them talk
      to Xero directly from here.

## Invitations

- [ ] `GET /invite` lists only pending invitations for the entity, gated on `USER_VIEW_ALL`.
- [ ] `POST /invite/cancel` marks an invitation `revoked` — row still exists afterward (never
      hard-deleted).
- [ ] `POST /invite` (send) proxies to Flask; a non-ASCII invite email surfaces Flask's 400
      unchanged through the wizard.

## Proxied to Flask

- [ ] `POST /modules` reflects subscription state as Flask computes it — not writable
      independently here.
- [ ] `POST /xero/disconnect` returns Flask's own status code verbatim (try it once with Xero
      actually connected, once already disconnected).

## Proxied to minty-subscription-api

- [ ] `GET /payment-method`, `GET/POST /billing/accounts`, `payment-methods*`, `authorize` all
      return the subscription API's status code unflattened — a declined card stays a 402, not
      remapped to a generic error.
- [ ] The hosted-Checkout routes (`payment-method/setup`, `/complete`) are gone — confirm they
      404, not proxy anywhere.

## Finalize (`POST /finalize {entity_id}`)

- [ ] Finalize with Xero connected flips the entity to `connected`.
- [ ] Finalize with no Xero connection flips the entity to `disconnected` (not an error).
- [ ] Every finalize call — including a retry — calls the subscription API's trial-start; a
      second finalize on an entity that already has a trial for a module does **not** create a
      duplicate trial row for that module.
- [ ] A non-200 from the subscription API during finalize surfaces that service's own status
      and error sentence, and leaves the company `connected`/`disconnected` (not reverted) so
      "Try again" on All Set only retries the trial start.
- [ ] Success response shape is exactly `{"status": "success", "trial_end": iso | null}`.

## The step-9 trap (read before testing any of the above by hand)

**Never GET `/state` or otherwise land a test/manual session on saved step 9 against an
entity you care about.** Arriving at step 9 is what finalizes the company and starts trial
subscriptions — the screen itself commits nothing because arrival already did. Use the
disposable entity pattern from `minty-onboarding-web/e2e/README.md` (a seeded `onboarding`
row, e.g. the dev `E2E Test Entity (do not use)`), and reset it afterward:

1. `POST /xero/disconnect` — sets `status` back to `onboarding`, nulls `xero_org_id`.
2. `POST /saved-step` with the value it held before the walk.
3. If the entity had modules enabled, clear any trial rows by hand:
   `DELETE FROM pettycashv3.entity_module_subscription WHERE entity_id = '<the id>';`

## Out of scope for this checklist

- **Xero's own OAuth / API behaviour** — covered by Minty's own tests, not this service.
- **The email OTP sign-in flow** — ends at a real inbox; not testable here or in the e2e
  suite. Token-verification behaviour (above) is what this service can actually check.

## See also

- [wizard-api.md](wizard-api.md) — the endpoint reference this checklist is derived from.
- [authentication.md](authentication.md) — the auth rules behind the first section.
- `minty-onboarding-web/e2e/README.md` — the automated browser suite, the real-vs-faked Xero
  table, and the step-9 incident this checklist's warning is based on.
