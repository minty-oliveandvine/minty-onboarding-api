# Authentication — minty-onboarding-api's half

This service **verifies tokens; it never mints them** (`core/auth.py`). Minty (Flask)
signs the person in — email OTP or Xero — mints the onboarding JWT and hands it to the
wizard in the launch URL; the system-wide picture is `Minty/docs/features/authentication.md`.
The repo `README.md` §Auth is the short version of this page.

## The bearer token

`Authorization: Bearer <jwt>` on every `/api/onboarding/*` endpoint except the public
reference ones (`server-time`, `currencies`, `countries`). `OnboardingBearerAuth`:

- HS256 over the **shared `SECRET_KEY`**; a malformed token, a bad signature, an expired
  one (`exp`, 60 minutes from Minty's `_mint_onboarding_token`, with 60 s of leeway for
  clock skew between hosts — `CLOCK_SKEW_LEEWAY_SECONDS`), a wrong `scope` (must be
  `"onboarding"`; `ACCEPT_ANY_SCOPE` is the documented escape hatch, `False`) and an
  unknown `user_id` are all 401. The message the wizard shows for an expired session comes from here.
- It answers only *who is this person*. **No role on an entity is required at the
  door**, unlike minty-payment-request-api: the first authenticated call, `POST /create`, exists to
  create the company the caller will then hold a role on. Every endpoint that names an
  entity checks membership itself — `core/permissions.entity_for_member(user_id,
  entity_id)` (a `user_entity` row, else 403 with Flask's wording).
- **The entity comes from the query string or the JSON body, never a header** — that is
  how Flask reads it and how the wizard sends it; `X-Entity-Id` is deliberately absent
  from `CORS_ALLOW_HEADERS` so a header nothing sends cannot be silently ignored.

## Permissions

`core/policy.py` is a port of Minty's `services/permission_policy.py` — the same roles
(`entity_base` … `super_admin`), the same `Permission` names, `has_permission`,
`is_superuser_readonly` (a system superuser with no row on the company can look, not
touch), `can_manage_role_assignment`. The petty-cash and invite endpoints gate on the
Minty permissions they port (`SALES_METHOD_VIEW` / `_CREATE`, `USER_VIEW_ALL`, …), so a
cashier who could not do it in Flask cannot do it here; `test_policy.py` pins the port
against the Flask matrix.

## What this service never does

- **Refresh a Xero token.** `core/xero_tokens.access_token_for(entity_id)` asks Minty's
  internal token service for a currently-valid token (the same assertion JWT
  minty-payment-request-api uses) and returns `None` when it cannot — *inconclusive*, never "not
  connected". `connected_tenant_ids` is the one read it makes against Xero.
- **Write to Stripe, subscriptions or `entity_function_map`.** Those endpoints proxy to
  Flask as the caller (`core/minty_client.forward` / `proxy`: the bearer forwarded
  verbatim, Flask's status returned unflattened — a 402 for a declined card stays a 402).
- **Send mail.** Invitations are sent by Flask; this service lists and cancels.

## Configuration

`APP_ENV` (`development` turns on DEBUG; anything else refuses the placeholder `SECRET_KEY`),
`SECRET_KEY` (shared), `PETTY_CASH_URL` (the proxies, and the token service derived from it:
`PETTY_CASH_URL` + `/api/internal/xero/token`), `DATABASE_URL` `?schema=` → `DB_SCHEMA` (the
`search_path`), `ONBOARDING_WEB_URL` / `CORS_ALLOWED_ORIGINS` for the wizard's origin.

## Tests

`tests/test_auth.py` (every refusal, the leeway), `tests/test_policy.py`,
`tests/test_minty_client.py` (the proxy keeps Flask's status), `tests/test_xero_tokens.py`;
in the browser `minty-onboarding-web/e2e/resume.spec.ts` (a forged, expired or wrong-scope token is
refused; a valid one opens the wizard).
