"""Logging configuration with ``key=value`` context appended to each record."""

from __future__ import annotations

import logging
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class ContextFormatter(logging.Formatter):
    """Append any ``extra={...}`` fields to the line as ``key=value`` pairs."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and v is not None}
        if not extras:
            return base
        context = " ".join(f"{key}={_fmt(value)}" for key, value in sorted(extras.items()))
        return f"{base} | {context}"


def _fmt(value: Any) -> str:
    text = str(value)
    return repr(text) if (" " in text or not text) else text


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(ContextFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party chatter that drowns out our own context-rich lines.
    for noisy in ("urllib3", "botocore", "boto3", "werkzeug", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
