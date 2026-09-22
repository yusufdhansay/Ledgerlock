"""Structured JSON logging.

Two requirements drive this module:

1. RULES.md: every rejected transaction is logged with a specific reason.
   Machine-readable output means the Phase 6 load test can count
   rejections *by reason* from the log stream, rather than guessing.
2. Security requirements: no secret, key, token, or password is ever
   logged, even at DEBUG. Relying on every future call site to remember
   that is not a control, so redaction is enforced here by a filter that
   every record passes through.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger.json import JsonFormatter

#: Substrings that mark a field as sensitive. Matched case-insensitively
#: against the field name, so `jwt_secret_key`, `Authorization`, and
#: `hashed_password` are all caught.
SENSITIVE_FIELD_MARKERS = (
    "password",
    "secret",
    "token",
    "authorization",
    "credential",
    "api_key",
    "apikey",
    "cookie",
    "mongodb_uri",
)

REDACTED = "[REDACTED]"

_CONFIGURED = False


def _is_sensitive(field_name: str) -> bool:
    lowered = field_name.lower()
    return any(marker in lowered for marker in SENSITIVE_FIELD_MARKERS)


class RedactSensitiveFields(logging.Filter):
    """Blank out sensitive values on every log record that passes through.

    This is a belt-and-braces control: call sites are not supposed to pass
    secrets in the first place, but a filter makes an accidental leak a
    non-event rather than an incident.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for field_name, value in list(record.__dict__.items()):
            if _is_sensitive(field_name) and value is not None:
                record.__dict__[field_name] = REDACTED
        return True


class LedgerlockJsonFormatter(JsonFormatter):
    """JSON formatter with stable top-level keys."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        # `event` mirrors the log message, which by convention in this
        # codebase is a snake_case event name such as `request_rejected`.
        log_record.setdefault("event", record.getMessage())


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger. Idempotent."""
    global _CONFIGURED

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        LedgerlockJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(RedactSensitiveFields())

    root = logging.getLogger()
    # Replace rather than append, so repeated calls (tests, reload) do not
    # produce duplicated lines.
    root.handlers = [handler]
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours so the
    # output stream stays uniformly JSON.
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. Safe to call at import time."""
    return logging.getLogger(name)
