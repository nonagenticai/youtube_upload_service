"""The entitlement gate on the tenant-facing routers actually refuses an
unlicensed tenant — and does NOT refuse Google's OAuth callback or a probe.

Why this file exists: ``youtube_upload_service`` is a BILLED destination (it is
listed in ``w2v_common.auth.outbound.METERED_DESTINATION_SERVICES``) and it
carried **no** gate code at all. ``AuthMiddleware`` introspects the licence
token for tenant IDENTITY — it answers "who are you", never "may you". A tenant
whose licence was ``expired`` or ``revoked`` still presented a syntactically
valid token, so ``/api/auth/*`` and ``/api/upload/*`` served them and the upload
was metered. ``LICENSE_GATE`` is mounted router-wide on both tenant-facing
routers to close that.

Every test here drives a REAL HTTP request through the REAL routers. Static
introspection of ``route.dependant.dependencies`` is NOT a valid check on this
FastAPI build: sub-routers are included lazily (``_IncludedRouter``), so a
parent router's ``dependencies=`` are applied when a request is matched, not
baked into the route objects at import time — introspection can report a
live-gated route as ungated. A request is the only honest probe.

The paired 200s (entitled tenant, and flag-off) are what stop this file from
passing vacuously: if the gate were mounted wrongly, or the fixtures broken,
those cases would not be 200s either.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from w2v_common.auth import clear_license_status_cache
from w2v_common.auth.licenses_read_client import LicensesClientError

from src.api.auth_routes import public_router as auth_public_router
from src.api.auth_routes import router as auth_router
from src.api.dependencies import (
    get_oauth_service,
    get_owner_tenant_id,
    get_redis_client,
    get_tenant_id,
    get_upload_service,
)
from src.api.health import router as health_router
from src.api.upload_routes import router as upload_router
from tests.tenants import tenant_uuid

TENANT = str(tenant_uuid("test-tenant"))
OWNER_TENANT = str(tenant_uuid("owner-tenant-7"))
SUBJECT = "user-under-test"

#: The cheapest genuinely tenant-scoped route on the /api/upload router: with a
#: faked UploadService, listing does no DB work of its own. So a non-200 here
#: can only be the gate — which is the whole point.
GATED_PATH_UPLOAD = "/api/upload/jobs"

#: The cheapest tenant-scoped route on the SECOND gated router (/api/auth).
#: Proves the gate is not mounted on one router only.
GATED_PATH_AUTH = "/api/auth/status"

#: Google's redirect target. It carries NO licence token and NO tenant identity
#: (the tenant is recovered from the one-time OAuth ``state`` in Redis), so it
#: MUST stay ungated: a 402 here breaks the consent flow for every tenant,
#: entitled or not. ``error=`` makes the handler answer 400 without touching
#: Redis or Google — a 402 instead of that 400 is the regression this pins.
UNGATED_CALLBACK_PATH = "/api/auth/callback?error=access_denied"

#: A probe. Must answer 200 for a revoked tenant: a 402/503 on a liveness probe
#: CrashLoops the pod, and an unlicensed tenant cannot be billed by killing our
#: own deployment.
PROBE_PATH = "/health/live"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Record:
    """A licences wire record. Only ``model_dump`` is on the gate's path."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def model_dump(self) -> dict[str, Any]:
        return dict(self._fields)


class _Detail(_Record):
    """An ACTIVE licence detail (the 200 from ``GET /licenses/active``)."""

    conflict = False


class _FakeLicenses:
    """Stands in for ``LicensesReader``.

    ``active`` None models the authoritative 404 ("no active licence"), which
    sends the gate to the subject inventory to refine none/expired/revoked.
    ``error`` models an upstream failure, which must 503, never allow.
    """

    def __init__(
        self,
        *,
        active: _Detail | None = None,
        inventory: list[_Record] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._active = active
        self._inventory = inventory or []
        self._error = error

    async def get_active_license(self, *, tenant_id: str, app_key: str) -> _Detail | None:
        if self._error is not None:
            raise self._error
        return self._active

    async def list_user_licenses(self, subject_id: str, *, tenant_id: str) -> list[_Record]:
        if self._error is not None:
            raise self._error
        return list(self._inventory)


def _revoked() -> list[_Record]:
    return [_Record(app={"key": "w2v"}, tenant_id=TENANT, status="revoked")]


def _expired() -> list[_Record]:
    return [
        _Record(
            app={"key": "w2v"},
            tenant_id=TENANT,
            status="expired",
            expires_at="2020-01-01T00:00:00+00:00",
        )
    ]


def _entitled() -> _Detail:
    return _Detail(
        app={"key": "w2v"},
        tenant_id=TENANT,
        status="active",
        expires_at="2099-01-01T00:00:00+00:00",
        plan={"code": "pro"},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_cached_verdicts():
    """The gate caches 60s per (tenant, app, subject). Two tests in one process
    would otherwise share a verdict and the second would assert nothing."""
    clear_license_status_cache()
    yield
    clear_license_status_cache()


@pytest.fixture(autouse=True)
def _gate_disarmed(monkeypatch):
    """Default the flag OFF, exactly as it ships. Each test arms it explicitly."""
    monkeypatch.delenv("W2V_LICENSE_GATE_ENABLED", raising=False)


@pytest.fixture
def mock_upload_service() -> MagicMock:
    service = MagicMock()
    service.list_jobs = AsyncMock(return_value=([], 0))
    return service


@pytest.fixture
def mock_oauth_service() -> MagicMock:
    service = MagicMock()
    service.get_token_info = AsyncMock(return_value=None)
    return service


@pytest.fixture
def app(mock_upload_service: MagicMock, mock_oauth_service: MagicMock) -> FastAPI:
    """The REAL routers, with identity stamped the way AuthMiddleware stamps it."""
    application = FastAPI()

    class _StampIdentity(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant_id = TENANT
            request.state.subject_id = SUBJECT
            return await call_next(request)

    application.add_middleware(_StampIdentity)
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(auth_public_router)
    application.include_router(upload_router)

    application.dependency_overrides[get_tenant_id] = lambda: TENANT
    application.dependency_overrides[get_owner_tenant_id] = lambda: OWNER_TENANT
    application.dependency_overrides[get_upload_service] = lambda: mock_upload_service
    application.dependency_overrides[get_oauth_service] = lambda: mock_oauth_service
    application.dependency_overrides[get_redis_client] = lambda: None
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _licenses(monkeypatch, fake: _FakeLicenses) -> None:
    """Patch the client the gate resolves through, in the module that calls it."""
    monkeypatch.setattr(
        "w2v_common.auth.entitlement.get_licenses_read_client", lambda *a, **k: fake
    )


# ---------------------------------------------------------------------------
# Armed: an unentitled tenant is refused (both gated routers)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inventory", "code"),
    [
        ([], "license_none"),
        (_revoked(), "license_revoked"),
        (_expired(), "license_expired"),
    ],
    ids=["none", "revoked", "expired"],
)
def test_armed_gate_402s_an_unentitled_tenant_on_upload_router(
    client: TestClient, monkeypatch, inventory, code
):
    """402 with the state-specific code — the tenant's licence is the problem."""
    monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "true")
    _licenses(monkeypatch, _FakeLicenses(active=None, inventory=inventory))

    response = client.get(GATED_PATH_UPLOAD)

    assert response.status_code == 402, response.text
    assert response.json()["detail"]["error"] == code


def test_armed_gate_402s_an_unentitled_tenant_on_auth_router(client: TestClient, monkeypatch):
    """Same refusal on the SECOND gated router. Connecting a channel is the
    front door to a billed upload; an unlicensed tenant must not walk through
    it either."""
    monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "true")
    _licenses(monkeypatch, _FakeLicenses(active=None, inventory=_revoked()))

    response = client.get(GATED_PATH_AUTH)

    assert response.status_code == 402, response.text
    assert response.json()["detail"]["error"] == "license_revoked"


def test_armed_gate_503s_when_entitlement_cannot_be_established(client: TestClient, monkeypatch):
    """An upstream failure is NOT an allow, and NOT a 402 either: we do not know."""
    monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "true")
    _licenses(monkeypatch, _FakeLicenses(error=LicensesClientError("licenses unreachable")))

    response = client.get(GATED_PATH_UPLOAD)

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "license_unavailable"
    assert response.headers["Retry-After"]


# ---------------------------------------------------------------------------
# Armed: an entitled tenant is served (the non-vacuity pair)
# ---------------------------------------------------------------------------


def test_armed_gate_serves_an_entitled_tenant(client: TestClient, monkeypatch):
    monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "true")
    _licenses(monkeypatch, _FakeLicenses(active=_entitled()))

    response = client.get(GATED_PATH_UPLOAD)

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


# ---------------------------------------------------------------------------
# Dormant on merge
# ---------------------------------------------------------------------------


def test_gate_is_dormant_while_the_flag_is_unset(client: TestClient, monkeypatch):
    """Merging this changes NOTHING until gitops arms the flag. A revoked tenant
    is still served — deliberately — so the rollout is a flag flip, not a deploy."""
    _licenses(monkeypatch, _FakeLicenses(active=None, inventory=_revoked()))

    response = client.get(GATED_PATH_UPLOAD)

    assert response.status_code == 200, response.text


def test_gate_is_dormant_when_the_flag_is_explicitly_false(client: TestClient, monkeypatch):
    monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "false")
    _licenses(monkeypatch, _FakeLicenses(active=None, inventory=_revoked()))

    response = client.get(GATED_PATH_UPLOAD)

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# What must NEVER be gated
# ---------------------------------------------------------------------------


def test_oauth_callback_is_not_gated_for_a_revoked_tenant(client: TestClient, monkeypatch):
    """Google redirects a browser here carrying no tenant identity at all. A 402
    would break the consent flow, and there is nothing to evaluate anyway. The
    handler's own 400 (Google reported ``error=``) is the proof it was reached."""
    monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "true")
    _licenses(monkeypatch, _FakeLicenses(active=None, inventory=_revoked()))

    response = client.get(UNGATED_CALLBACK_PATH)

    assert response.status_code == 400, response.text
    assert "Authorization Failed" in response.text


def test_probe_is_served_for_a_revoked_tenant(client: TestClient, monkeypatch):
    """A gated probe path CrashLoops the pod. /health/live must stay 200."""
    monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "true")
    _licenses(monkeypatch, _FakeLicenses(active=None, inventory=_revoked()))

    response = client.get(PROBE_PATH)

    assert response.status_code == 200, response.text


def test_the_services_own_probe_paths_are_exempt_by_path():
    """Belt and braces: even if a future refactor mounted the gate on the health
    router, w2v_common would still refuse to enforce on these paths. No
    hand-rolled exemption list exists in this service — this is the fleet one."""
    from w2v_common.auth import is_license_exempt_path

    for path in ("/health", "/health/live", "/health/ready", "/healthz", "/readyz"):
        assert is_license_exempt_path(path), path
    assert not is_license_exempt_path(GATED_PATH_UPLOAD)
    assert not is_license_exempt_path(GATED_PATH_AUTH)
