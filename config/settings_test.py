from config.settings import *  # noqa: F401, F403

# A fixed key so tests can mint a JWT the service will accept. Nothing about the
# value matters except that signing and verifying use the same one -- which is the
# whole contract with Flask in production, too.
SECRET_KEY = "test-secret-key-shared-with-flask"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

LOGGING["handlers"]["file_core"] = {"class": "logging.NullHandler"}  # noqa: F405
LOGGING["handlers"]["file_api"] = {"class": "logging.NullHandler"}  # noqa: F405

# Build the shared tables in the test database. In production they are Alembic's.
SHARED_MODELS_MANAGED_FOR_TESTING = True

# No test may reach the real Flask app. Proxy tests stub the transport; this value
# exists so an un-stubbed call fails fast against an obviously fake host instead of
# quietly hitting a developer localhost.
FLASK_APP_URL = "http://flask.invalid"
