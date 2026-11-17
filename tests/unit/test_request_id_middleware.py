"""Tests for RequestIdMiddleware — trace continuity glue to w2v_common."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from w2v_common.middleware import RequestIdMiddleware
from w2v_common.observability.request_context import (
    clear_request_id,
    get_request_id,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/probe")
    async def probe() -> dict[str, str]:
        return {"request_id": get_request_id()}

    return app


def test_preserves_inbound_header():
    clear_request_id()
    client = TestClient(_build_app())
    resp = client.get("/probe", headers={"X-Request-Id": "trace-abc-123"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "trace-abc-123"
    assert resp.json()["request_id"] == "trace-abc-123"


def test_generates_when_missing():
    clear_request_id()
    client = TestClient(_build_app())
    resp = client.get("/probe")
    assert resp.status_code == 200
    rid = resp.headers["X-Request-Id"]
    assert rid
    assert len(rid) >= 32
    assert resp.json()["request_id"] == rid


def test_binding_visible_to_handler():
    clear_request_id()
    client = TestClient(_build_app())
    resp = client.get("/probe", headers={"X-Request-Id": "trace-xyz"})
    assert resp.json()["request_id"] == "trace-xyz"


def test_case_insensitive_header():
    clear_request_id()
    client = TestClient(_build_app())
    resp = client.get("/probe", headers={"x-request-id": "lowercase-id"})
    assert resp.headers["X-Request-Id"] == "lowercase-id"
    assert resp.json()["request_id"] == "lowercase-id"
