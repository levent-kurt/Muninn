"""Structured logging.

Plain text log lines are fine for a terminal and useless for grep across a
long-running service. ``LOG_FORMAT=json`` switches every Muninn logger to one
JSON object per line, which is what journald, Loki, CloudWatch and most log
shippers want, without adding a logging dependency.

The formatter is deliberately minimal: standard LogRecord attributes plus any
``extra`` the call site passed. It never formats a traceback into the message
- that stays on the exception, where it belongs.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

LOG_FORMATS = ("text", "json")

# Attributes present on every LogRecord; anything else was passed via `extra`.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Configure the root logger for the whole process.

    Idempotent: calling it twice does not stack handlers.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
