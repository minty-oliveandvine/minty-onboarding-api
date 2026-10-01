# Features — the onboarding wizard's API

`onboarding-backend` is the Django (Ninja) service the onboarding wizard talks to,
extracted from Minty one endpoint group at a time. It verifies the token Minty minted,
reads and writes the wizard's own tables in the shared schema (`MINTY_DB_SCHEMA`, default
`pettycashv3`, every model `managed = False`), and proxies to Flask whatever touches a
rail only Flask may write — Stripe, Xero tokens, subscriptions, mail.

| Feature | Document |
|---|---|
| Verifying the onboarding token, membership per endpoint, the permission port, what this service never does | [authentication.md](authentication.md) — Minty's `docs/features/authentication.md` has the system-wide picture |
| Every `/api/onboarding/*` endpoint: reference data, resume state, the company, petty-cash setup, invitations, the proxies | [wizard-api.md](wizard-api.md) |

The three rules the service is built on (a SQLAlchemy `default=` is invisible to Django;
Alembic owns the schema; whoever owns the external rail owns the write), the migration
groups and the deliberate divergences from Flask are in the repo `README.md`; read it
first. Running it: `manage.py runserver 8001` with `.env` (`SECRET_KEY` shared with Minty,
`FLASK_APP_URL`, `ONBOARDING_APP_URL`, `MINTY_DB_SCHEMA`); tests
`pytest` (371 + 1 skipped on 2026-10-01; needs `MINTY_REPO` for the schema harness).
