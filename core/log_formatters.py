import logging
from datetime import datetime, timezone


class CoreFormatter(logging.Formatter):
    """
    Core log format:
    2026-09-11T08:42:15Z INFO minty-onboarding Entity created
    """

    def format(self, record):
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return f"{ts} {record.levelname} {record.name} {record.getMessage()}"


class ApiFormatter(logging.Formatter):
    """
    API log format:
    2026-09-11T08:42:15Z INFO minty-onboarding.http request_id=req_92fa
        user_id=128 endpoint=/api/onboarding/state method=GET 200 14ms
    """

    def format(self, record):
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        base = f"{ts} {record.levelname} {record.name}"
        extras = ""
        for attr in ("request_id", "user_id", "endpoint", "method"):
            val = getattr(record, attr, None)
            if val is not None:
                extras += f" {attr}={val}"
        return f"{base}{extras} {record.getMessage()}"
