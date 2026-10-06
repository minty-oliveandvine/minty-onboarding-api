# Features — the onboarding wizard's API

`minty-onboarding-api` is the Django (Ninja) service the onboarding wizard talks to,
extracted from Minty one endpoint group at a time. It verifies the token Minty minted,
reads and writes the wizard's own tables in the shared schema (`?schema=` on `DATABASE_URL`,
default `pettycashv3`, every model `managed = False`), and proxies whatever touches a rail it
may not write — Stripe and subscriptions to minty-subscription-api (since 2026-10-06), Xero
tokens, module grants and mail to Flask.

| Feature | Document |
|---|---|
| Verifying the onboarding token, membership per endpoint, the permission port, what this service never does | [authentication.md](authentication.md) — Minty's `docs/features/authentication.md` has the system-wide picture |
| Every `/api/onboarding/*` endpoint: reference data, resume state, the company, petty-cash setup, invitations, the proxies | [wizard-api.md](wizard-api.md) |

The three rules the service is built on (a SQLAlchemy `default=` is invisible to Django;
Alembic owns the schema; whoever owns the external rail owns the write), the migration
groups and the deliberate divergences from Flask are in the repo `README.md`; read it
first. Running it: `manage.py runserver 8030` with `.env` (`APP_ENV`, `SECRET_KEY` shared with Minty,
`DATABASE_URL`, `PETTY_CASH_URL`, `SUBSCRIPTION_API_URL`, `ONBOARDING_WEB_URL`; see `.env.example`); tests
`pytest` (399 + 1 skipped on 2026-10-06; needs `MINTY_REPO` for the schema harness).
