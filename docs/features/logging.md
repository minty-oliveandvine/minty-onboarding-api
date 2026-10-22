# Logging

Every line this service writes, like every other Minty backend's (Flask included), has one format,
defined once in [`minty-shared-py`](https://github.com/minty-oliveandvine/minty-shared-py)
(`minty_shared.logctx`, pinned in `requirements.txt`; since 2026-10-22):

```
2026-10-22T08:42:15.123Z INFO [trace=a1b2c3d4] [user=<uuid>|entity=<uuid>|step=get_state] message
```

`-` stands for anything not known. `config/settings.py` builds the `LOGGING` dict with
`logging_config`:

| Logger | Writes | File |
|---|---|---|
| `minty-onboarding` | everything the service logs | `logs/core.log` |
| `minty-onboarding.http` | one access line per request, `METHOD path status ms` (does not propagate) | `logs/api.log` |

Both also go to stderr, which is what Render keeps.

- **trace**: `core.middleware.RequestLoggingMiddleware` opens one per request. An incoming
  `X-Request-ID` of 8-32 lowercase hex digits is kept; anything else (missing, uppercase, a
  newline, a forged `[user=...]`) is replaced with 8 fresh digits. The id is echoed on the
  response as `X-Request-ID` and kept on `request.request_id`.
- **user / entity**: bound only once they are verified. `core.auth.OnboardingBearerAuth` binds the person; the company is bound by the entity gate, `core.permissions.entity_for_member`, once membership is confirmed (the wizard names its company per request, not in the token).
- **step**: the route's URL name (ninja names every operation) unless code names one with
  `with logctx.log_step("area.action"):` or `@logctx.log_step(...)`. No step is named in code yet; the route name says what each request does.
- **Calls to Flask** send the trace (`logctx.outgoing_headers()`), so Flask's lines for the same
  request share it: `core/minty_client.py::forward` and `core/xero_tokens.py::access_token_for` (Flask's token service; the `/connections` call to Xero sends none). Calls to Xero and Stripe never do.
- **Context is cleared** at the end of each request (`logctx.reset()`), so a reused worker never
  prints the previous caller.

Tests: `tests/test_logging.py`. When you add a log call, keep ids the line already carries out of the message;
a row id (`bill=…`, `link=…`) still belongs in it.
