"""Tests for structured logging configuration."""

from __future__ import annotations

import json
import logging

import pytest
from w2v_common.observability.request_context import (
    clear_request_id,
    set_request_id,
)

from src.logging_config import (
    ContextFormatter,
    JSONFormatter,
    configure_logging,
    generate_request_id,
    job_id_var,
    tenant_id_var,
)


@pytest.mark.unit
class TestJSONFormatter:
    def test_formats_as_json(self) -> None:
        formatter = JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "hello"
        assert data["logger"] == "test"
        assert "timestamp" in data

    def test_includes_context_vars(self) -> None:
        formatter = JSONFormatter()
        set_request_id("req-123")
        token_tid = tenant_id_var.set("tenant-abc")
        token_jid = job_id_var.set("job-456")
        try:
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="with context",
                args=(),
                exc_info=None,
            )
            output = formatter.format(record)
            data = json.loads(output)
            assert data["request_id"] == "req-123"
            assert data["tenant_id"] == "tenant-abc"
            assert data["job_id"] == "job-456"
        finally:
            clear_request_id()
            tenant_id_var.reset(token_tid)
            job_id_var.reset(token_jid)

    def test_omits_empty_context_vars(self) -> None:
        formatter = JSONFormatter()
        clear_request_id()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="no context",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "request_id" not in data
        assert "tenant_id" not in data
        assert "job_id" not in data

    def test_includes_exception_info(self) -> None:
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg="error",
                args=(),
                exc_info=exc_info,
            )
        output = formatter.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]


@pytest.mark.unit
class TestContextFormatter:
    def test_formats_with_context(self) -> None:
        formatter = ContextFormatter(
            fmt="%(levelname)s - %(ctx)s%(message)s",
        )
        set_request_id("req-abc")
        token_tid = tenant_id_var.set("t-1")
        try:
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="hello",
                args=(),
                exc_info=None,
            )
            output = formatter.format(record)
            assert "req=req-abc" in output
            assert "tenant=t-1" in output
            assert "hello" in output
        finally:
            clear_request_id()
            tenant_id_var.reset(token_tid)

    def test_formats_without_context(self) -> None:
        formatter = ContextFormatter(
            fmt="%(levelname)s - %(ctx)s%(message)s",
        )
        clear_request_id()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="no context",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "[" not in output
        assert "no context" in output

    def test_includes_job_id(self) -> None:
        formatter = ContextFormatter(fmt="%(ctx)s%(message)s")
        token_jid = job_id_var.set("j-99")
        try:
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="msg",
                args=(),
                exc_info=None,
            )
            output = formatter.format(record)
            assert "job=j-99" in output
        finally:
            job_id_var.reset(token_jid)


@pytest.mark.unit
class TestConfigureLogging:
    def test_configure_json_logging(self) -> None:
        configure_logging(json_logs=True, level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JSONFormatter)

    def test_configure_text_logging(self) -> None:
        configure_logging(json_logs=False, level="WARNING")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert isinstance(root.handlers[0].formatter, ContextFormatter)

    def test_replaces_existing_handlers(self) -> None:
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler())
        root.addHandler(logging.StreamHandler())
        configure_logging(json_logs=False, level="INFO")
        assert len(root.handlers) == 1


@pytest.mark.unit
class TestGenerateRequestId:
    def test_returns_12_char_hex(self) -> None:
        rid = generate_request_id()
        assert len(rid) == 12
        int(rid, 16)  # validates it's hex

    def test_unique(self) -> None:
        ids = {generate_request_id() for _ in range(100)}
        assert len(ids) == 100
