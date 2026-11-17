"""Structured logging configuration with request context propagation."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from w2v_common.observability.request_context import get_request_id

tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="")
job_id_var: ContextVar[str] = ContextVar("job_id", default="")


class JSONFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = get_request_id()
        if rid:
            log_entry["request_id"] = rid
        tid = tenant_id_var.get("")
        if tid:
            log_entry["tenant_id"] = tid
        jid = job_id_var.get("")
        if jid:
            log_entry["job_id"] = jid
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


class ContextFormatter(logging.Formatter):
    """Text formatter that includes context vars when available."""

    def format(self, record: logging.LogRecord) -> str:
        parts = []
        rid = get_request_id()
        if rid:
            parts.append(f"req={rid}")
        tid = tenant_id_var.get("")
        if tid:
            parts.append(f"tenant={tid}")
        jid = job_id_var.get("")
        if jid:
            parts.append(f"job={jid}")
        ctx = " ".join(parts)
        if ctx:
            record.ctx = f"[{ctx}] "
        else:
            record.ctx = ""
        return super().format(record)


def configure_logging(json_logs: bool = False, level: str = "INFO") -> None:
    """Replace default logging config with structured logging."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))
    else:
        handler.setFormatter(
            ContextFormatter(
                fmt="%(asctime)s - %(name)s - %(levelname)s - %(ctx)s%(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)


def generate_request_id() -> str:
    """Generate a short request ID."""
    return uuid.uuid7().hex[-12:]
