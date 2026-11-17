"""Guardrail tests for the auth middleware integration.

youtube_upload_service delegates auth to the shared
``w2v_common.auth.middleware.AuthMiddleware``, adopted via
``w2v_common.asgi.create_app``. The OAuth2 redirect target
``/api/auth/callback`` (which Google posts to without a license token) is
declared public through the factory's ``auth_public_endpoints`` parameter —
NOT a per-service subclass and NOT a mutation of the shared
``PUBLIC_ENDPOINTS`` set.

These tests catch the "false-merge" pattern: a PR that claims to wire auth but
stops calling ``create_app``, or that drops the ``/api/auth/callback`` public
exemption (which would 401 every OAuth callback in production).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest

# Same shared module the asgi entry point (via create_app) enforces against.
import w2v_common.auth.middleware as w2v_auth
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from w2v_common.auth.middleware import AuthMiddleware

from tests.tenants import tenant_uuid

# The path youtube declares public via create_app(auth_public_endpoints=...).
CALLBACK_PATH = "/api/auth/callback"


@pytest.fixture(autouse=True)
def _clear_license_cache() -> None:
    cache = w2v_auth._get_license_cache()
    cache.clear()


def _make_minimal_app() -> FastAPI:
    """Tiny app wired exactly as production: shared AuthMiddleware + the
    OAuth-callback public exemption passed as config."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, extra_public_endpoints={CALLBACK_PATH})

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/upload/echo")
    async def _echo(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "tenant_id": getattr(request.state, "tenant_id", None),
                "license_present": getattr(request.state, "license", None) is not None,
            }
        )

    @app.get(CALLBACK_PATH)
    async def _cb() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Wiring check — the regression-against-false-merge assertion
# ---------------------------------------------------------------------------


def test_asgi_adopts_create_app_with_callback_public() -> None:
    """asgi.py must build the app via ``create_app`` and declare the OAuth
    callback public through ``auth_public_endpoints``.

    Tests run with ``AUTH_ENABLED=false`` (conftest), so the live ``app`` does
    not carry AuthMiddleware — the wiring is asserted at the source level (the
    factory's own tests prove ``auth_public_endpoints`` reaches enforcement +
    OpenAPI). Drift here (dropping the factory or the callback exemption) is a
    production-breaking change and must fail loudly.
    """
    asgi_src = (
        __import__("pathlib").Path(__file__).resolve().parent.parent / "src" / "asgi.py"
    ).read_text()
    assert "create_app(" in asgi_src, "asgi.py must build the app via w2v_common.asgi.create_app"
    assert 'auth_public_endpoints={"/api/auth/callback"}' in asgi_src, (
        "asgi.py must declare /api/auth/callback public via create_app(auth_public_endpoints=...)"
    )


def test_create_app_registers_shared_auth_and_exempts_callback() -> None:
    """Building the app the way youtube does (create_app + auth_public_endpoints,
    auth forced on) registers the shared AuthMiddleware and marks the callback
    public in the OpenAPI schema."""
    from w2v_common.asgi import create_app
    from w2v_common.auth.middleware import AuthMiddleware as SharedAuthMiddleware

    from src.config import get_settings

    app = create_app(
        get_settings(),
        routers=[],
        auth_enabled=True,
        auth_public_endpoints={CALLBACK_PATH},
    )
    assert SharedAuthMiddleware in {m.cls for m in app.user_middleware}

    @app.get(CALLBACK_PATH, operation_id="cb")
    async def _cb() -> dict[str, str]:
        return {"ok": "yes"}

    schema = app.openapi()
    assert "security" not in schema["paths"][CALLBACK_PATH]["get"]


# ---------------------------------------------------------------------------
# Public bypass
# ---------------------------------------------------------------------------


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------


async def test_protected_route_with_valid_license_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    introspect_payload: dict[str, Any] = {
        "active": True,
        "tenant_id": tenant_uuid("tenant-1"),
        "subject_id": "subj-1",
        "license_id": "lic-1",
        "roles": ["user"],
    }

    async def _fake(token: str) -> dict[str, Any]:
        assert token == "abc.def.ghi"
        return introspect_payload

    monkeypatch.setattr(w2v_auth, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/upload/echo",
            headers={"X-Tenant-Id": "Bearer abc.def.ghi"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == tenant_uuid("tenant-1")
    assert body["license_present"] is True


async def test_protected_route_with_invalid_license_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake(_token: str) -> dict[str, Any]:
        return {"active": False, "reason": "expired"}

    monkeypatch.setattr(w2v_auth, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/upload/echo",
            headers={"X-Tenant-Id": "Bearer dud-token"},
        )

    assert resp.status_code == 401
    body = resp.json()
    # w2v_common >= 0.16 emits the standard envelope shape on 401.
    assert body["error"]["code"] == "INVALID_TOKEN"
    assert body["error"]["status"] == 401
    assert "Invalid or expired license token" in body["error"]["message"]


async def test_protected_route_no_token_untrusted_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untrusted source IP + no token must 401, regardless of TRUST_INCLUSTER_IDENTITY."""
    # Force the trust-incluster path off so the test is deterministic.
    monkeypatch.setattr(w2v_auth, "_trust_incluster_identity", lambda: False)

    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/upload/echo")
    # ASGITransport peer = 'testclient' which is NOT in the trusted-CIDR list,
    # so this should hit the 401 branch.
    assert resp.status_code == 401


async def test_oauth_callback_is_public_via_auth_public_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/api/auth/callback`` is the Google redirect target — must bypass auth.

    The shared AuthMiddleware, given ``extra_public_endpoints={callback}``,
    short-circuits this path before any token check. Validates the config-driven
    exemption that replaced the old per-service subclass.
    """
    monkeypatch.setattr(w2v_auth, "_trust_incluster_identity", lambda: False)

    app = FastAPI()
    app.add_middleware(AuthMiddleware, extra_public_endpoints={CALLBACK_PATH})

    @app.get(CALLBACK_PATH)
    async def _cb() -> dict[str, str]:
        return {"ok": "yes"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(CALLBACK_PATH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": "yes"}
