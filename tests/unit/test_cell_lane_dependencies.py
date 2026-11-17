"""P7 cell lane on the orchestrator-only routes (owner tenant, actor, operator gate).

Real w2v_common ``AuthMiddleware`` with a test Ed25519 JWKS, the REAL
``/api/settings`` + ``/api/admin`` routers and the REAL upload-route
dependencies. Only the config service (DB) is faked, as a call recorder.

Before this change ``caller_is_orchestrator_sa`` read ``request.state.license``
only, which the cell lane sets to ``None``: under ``CELL_AUTH_MODE=enforce``
every orchestrator call here was a 403.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import w2v_common.auth.middleware as w2v_auth
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI
from jwt.algorithms import OKPAlgorithm
from w2v_common.auth.cell_jwt import CELL_TOKEN_HEADER, CELL_TOKEN_TYPE, reset_cell_state
from w2v_common.auth.identity_context import clear_identity_context
from w2v_common.auth.middleware import AuthMiddleware

from src.api.dependencies import get_config_service, get_owner_tenant_id, get_tenant_id
from src.api.settings_routes import admin_router
from src.api.settings_routes import router as settings_router
from tests.orchestrator_sa import SA_SUBJECT, allow_orchestrator_sa
from tests.tenants import tenant_uuid

SELF = "youtube-upload-service"
DEPLOYMENT = "strato-w2v"
ORCH = "w2v-orchestrator"
ANALYSIS = "analysis-service"
OWNER_A = tenant_uuid("owner-a")
OWNER_B = tenant_uuid("owner-b")
PLATFORM = tenant_uuid("platform")
SA_TENANT = tenant_uuid("orchestrator-sa")
ACTOR = "0198f1d2-0000-7000-8000-0000000ac701"
SPOOF_ACTOR = "0198f1d2-0000-7000-8000-0000000bad01"


def _jwk(private: Ed25519PrivateKey, kid: str, iss: str) -> dict[str, Any]:
    entry = json.loads(OKPAlgorithm.to_jwk(private.public_key()))
    entry.update({"kid": kid, "iss": iss})
    return entry


@pytest.fixture(scope="module")
def keys() -> dict[str, Ed25519PrivateKey]:
    return {ORCH: Ed25519PrivateKey.generate(), ANALYSIS: Ed25519PrivateKey.generate()}


@pytest.fixture
def cell_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys: dict[str, Ed25519PrivateKey]
) -> Iterator[None]:
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps({"keys": [_jwk(k, f"{iss}-1", iss) for iss, k in keys.items()]}))
    for name in ("CELL_JWT_SIGNING_KEY", "CELL_JWT_KID", "SERVICE_ACTOR_ID", "W2V_LOCAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CELL_SERVICE_NAME", SELF)
    monkeypatch.setenv("CELL_DEPLOYMENT_ID", DEPLOYMENT)
    monkeypatch.setenv("CELL_JWKS_PATH", str(jwks))
    # Both issuers pass the middleware allow-list, so a refusal below is THIS
    # service's orchestrator-only check, not w2v_common's CELL_ALLOWED_CALLERS.
    monkeypatch.setenv("CELL_ALLOWED_CALLERS", f"{ORCH},{ANALYSIS}")
    monkeypatch.setenv("CELL_AUTH_MODE", "enforce")
    monkeypatch.setattr(w2v_auth, "_trust_incluster_identity", lambda: False)
    w2v_auth._get_license_cache().clear()
    reset_cell_state()
    clear_identity_context()
    yield
    reset_cell_state()
    clear_identity_context()


def _cell_token(
    keys: dict[str, Ed25519PrivateKey], *, iss: str = ORCH, tenant: str = OWNER_A, actor: str | None
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": SELF,
        "cell": DEPLOYMENT,
        "scope": "tenant",
        "tenant": tenant,
        "run": None,
        "actor": actor,
        "project": None,
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": uuid.uuid7().hex,
    }
    return jwt.encode(
        claims, keys[iss], algorithm="EdDSA", headers={"kid": f"{iss}-1", "typ": CELL_TOKEN_TYPE}
    )


class _RecordingConfig:
    """Stands in for ``YouTubeConfigService``: records (method, tenant, actor)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    @staticmethod
    def _view() -> dict[str, Any]:
        return {
            "oauth_client_id": None,
            "has_oauth_client_secret": False,
            "redirect_uri": "http://localhost:6315/api/auth/callback",
            "effective_platform_channel_allowed": False,
        }

    async def settings_view(self, tenant_id: str) -> dict[str, Any]:
        self.calls.append(("settings_view", tenant_id, None))
        return self._view()

    async def clear_oauth_client(self, tenant_id: str, actor: str) -> dict[str, Any]:
        self.calls.append(("clear_oauth_client", tenant_id, actor))
        return self._view()

    async def set_tenant_platform_allowed(
        self, tenant_id: str, allowed: bool | None, actor: str
    ) -> dict[str, Any]:
        self.calls.append(("set_tenant_platform_allowed", tenant_id, actor))
        return {
            "tenant_id": tenant_id,
            "platform_channel_allowed": allowed,
            "effective_platform_channel_allowed": bool(allowed),
        }


@pytest.fixture
def config() -> _RecordingConfig:
    return _RecordingConfig()


@pytest.fixture
async def client(cell_env: None, config: _RecordingConfig) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(settings_router)
    app.include_router(admin_router)
    app.dependency_overrides[get_config_service] = lambda: config

    @app.get("/api/upload/_owner-probe")
    async def _probe(
        channel: str = Depends(get_tenant_id), owner: str = Depends(get_owner_tenant_id)
    ) -> dict[str, str]:
        """The exact dependency pair every ``/api/upload*`` route takes."""
        return {"channel": channel, "owner": owner}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _cell(keys: dict[str, Ed25519PrivateKey], **kw: Any) -> dict[str, str]:
    kw.setdefault("actor", ACTOR)
    return {CELL_TOKEN_HEADER: _cell_token(keys, **kw)}


# --------------------------------------------------------------------------- #
# enforce: cell lane only, no licence anywhere
# --------------------------------------------------------------------------- #


async def test_orchestrator_cell_token_authorizes_tenant_routes_with_the_signed_owner(
    client: httpx.AsyncClient, config: _RecordingConfig, keys: dict[str, Ed25519PrivateKey]
) -> None:
    resp = await client.get("/api/settings", headers=_cell(keys))
    assert resp.status_code == 200, resp.text
    resp = await client.delete("/api/settings/oauth-client", headers=_cell(keys))
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/upload/_owner-probe", headers=_cell(keys))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"channel": OWNER_A, "owner": OWNER_A}
    assert config.calls == [
        ("settings_view", OWNER_A, None),
        ("clear_oauth_client", OWNER_A, ACTOR),
    ]


async def test_spoofed_owner_and_actor_headers_are_ignored_on_the_cell_lane(
    client: httpx.AsyncClient, config: _RecordingConfig, keys: dict[str, Ed25519PrivateKey]
) -> None:
    spoof = {"X-Owner-Tenant-Id": OWNER_B, "X-Actor-Id": SPOOF_ACTOR}
    resp = await client.delete("/api/settings/oauth-client", headers={**_cell(keys), **spoof})
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/upload/_owner-probe", headers={**_cell(keys), **spoof})
    assert resp.status_code == 200, resp.text
    assert resp.json()["owner"] == OWNER_A
    assert config.calls == [("clear_oauth_client", OWNER_A, ACTOR)]


async def test_null_actor_records_the_calling_service(
    client: httpx.AsyncClient, config: _RecordingConfig, keys: dict[str, Ed25519PrivateKey]
) -> None:
    resp = await client.delete("/api/settings/oauth-client", headers=_cell(keys, actor=None))
    assert resp.status_code == 200, resp.text
    assert config.calls == [("clear_oauth_client", OWNER_A, f"cell:{ORCH}")]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings"),
        ("DELETE", "/api/settings/oauth-client"),
        ("GET", "/api/upload/_owner-probe"),
        ("PUT", f"/api/admin/tenants/{OWNER_B}/platform-channel"),
    ],
)
async def test_a_non_orchestrator_cell_caller_is_refused(
    client: httpx.AsyncClient,
    config: _RecordingConfig,
    keys: dict[str, Ed25519PrivateKey],
    method: str,
    path: str,
) -> None:
    resp = await client.request(
        method, path, headers=_cell(keys, iss=ANALYSIS), json={"allowed": True}
    )
    assert resp.status_code == 403, resp.text
    assert config.calls == []


def _service_token(keys: dict[str, Ed25519PrivateKey], *, iss: str = ORCH) -> str:
    """A ``scope=service`` token: no tenant, run, actor or project."""
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": SELF,
        "cell": DEPLOYMENT,
        "scope": "service",
        "tenant": None,
        "run": None,
        "actor": None,
        "project": None,
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": uuid.uuid7().hex,
    }
    return jwt.encode(
        claims, keys[iss], algorithm="EdDSA", headers={"kid": f"{iss}-1", "typ": CELL_TOKEN_TYPE}
    )


def _service(keys: dict[str, Ed25519PrivateKey], *, iss: str = ORCH) -> dict[str, str]:
    return {CELL_TOKEN_HEADER: _service_token(keys, iss=iss)}


ADMIN_PUT = f"/api/admin/tenants/{OWNER_B}/platform-channel"


@pytest.mark.parametrize("armed", [False, True], ids=["gate-dormant", "gate-armed"])
async def test_operator_route_authorizes_the_orchestrator_service_token(
    client: httpx.AsyncClient,
    config: _RecordingConfig,
    keys: dict[str, Ed25519PrivateKey],
    monkeypatch: pytest.MonkeyPatch,
    armed: bool,
) -> None:
    """updated_by = X-Actor-Id (attribution only); the tenant-less service lane
    is not run through the tenant licence gate even when it is armed."""
    if armed:
        monkeypatch.setenv("W2V_LICENSE_GATE_ENABLED", "true")
    resp = await client.put(
        ADMIN_PUT, headers={**_service(keys), "X-Actor-Id": ACTOR}, json={"allowed": True}
    )
    assert resp.status_code == 200, resp.text
    assert config.calls == [("set_tenant_platform_allowed", OWNER_B, ACTOR)]


async def test_operator_service_token_without_actor_header_records_the_service(
    client: httpx.AsyncClient, config: _RecordingConfig, keys: dict[str, Ed25519PrivateKey]
) -> None:
    resp = await client.put(ADMIN_PUT, headers=_service(keys), json={"allowed": False})
    assert resp.status_code == 200, resp.text
    assert config.calls == [("set_tenant_platform_allowed", OWNER_B, f"cell:{ORCH}")]


async def test_operator_route_refuses_a_service_token_from_another_issuer(
    client: httpx.AsyncClient, config: _RecordingConfig, keys: dict[str, Ed25519PrivateKey]
) -> None:
    """Refused by w2v_common before routing (``service_caller_not_allowed``):
    in enforce an invalid cell token is a 401, not a handler 403."""
    resp = await client.put(ADMIN_PUT, headers=_service(keys, iss=ANALYSIS), json={"allowed": True})
    assert resp.status_code == 401, resp.text
    assert config.calls == []


async def test_operator_route_refuses_a_tenant_scope_cell_token(
    client: httpx.AsyncClient, config: _RecordingConfig, keys: dict[str, Ed25519PrivateKey]
) -> None:
    resp = await client.put(ADMIN_PUT, headers=_cell(keys, tenant=PLATFORM), json={"allowed": True})
    assert resp.status_code == 403, resp.text
    assert "service-scope" in resp.json()["detail"]
    assert config.calls == []


async def test_operator_route_still_refuses_a_tenant_scoped_call(
    client: httpx.AsyncClient, config: _RecordingConfig, keys: dict[str, Ed25519PrivateKey]
) -> None:
    resp = await client.put(
        ADMIN_PUT, headers={**_service(keys), "X-Owner-Tenant-Id": OWNER_A}, json={"allowed": True}
    )
    assert resp.status_code == 403, resp.text
    assert config.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings"),
        ("DELETE", "/api/settings/oauth-client"),
        ("GET", "/api/upload/_owner-probe"),
    ],
)
async def test_tenant_routes_refuse_an_orchestrator_service_token(
    client: httpx.AsyncClient,
    config: _RecordingConfig,
    keys: dict[str, Ed25519PrivateKey],
    method: str,
    path: str,
) -> None:
    resp = await client.request(
        method, path, headers={**_service(keys), "X-Owner-Tenant-Id": OWNER_A}
    )
    assert resp.status_code == 401, resp.text
    assert config.calls == []


async def test_enforce_without_any_credential_is_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/settings", headers={"X-Owner-Tenant-Id": OWNER_A})
    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------- #
# dual: the licence lane is unchanged
# --------------------------------------------------------------------------- #


def _licence(monkeypatch: pytest.MonkeyPatch, subject: str) -> dict[str, str]:
    async def _introspect(_token: str) -> dict[str, Any]:
        return {
            "active": True,
            "tenant_id": SA_TENANT,
            "subject_id": subject,
            "license_id": "lic-1",
            "roles": [],
        }

    monkeypatch.setattr(w2v_auth, "_introspect_license", _introspect)
    return {"X-Tenant-Id": "Bearer licence.token.value"}


@pytest.fixture
def dual(monkeypatch: pytest.MonkeyPatch, cell_env: None) -> None:
    monkeypatch.setenv("CELL_AUTH_MODE", "dual")
    allow_orchestrator_sa(monkeypatch)


async def test_dual_sa_licence_without_cell_token_keeps_the_header_owner(
    dual: None,
    client: httpx.AsyncClient,
    config: _RecordingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {
        **_licence(monkeypatch, SA_SUBJECT),
        "X-Owner-Tenant-Id": OWNER_B,
        "X-Actor-Id": ACTOR,
    }
    resp = await client.delete("/api/settings/oauth-client", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/upload/_owner-probe", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"channel": SA_TENANT, "owner": OWNER_B}
    assert config.calls == [("clear_oauth_client", OWNER_B, ACTOR)]


async def test_dual_customer_licence_is_still_refused(
    dual: None,
    client: httpx.AsyncClient,
    config: _RecordingConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    customer = "0198f1d2-0000-7000-8000-0000000c5501"
    headers = {**_licence(monkeypatch, customer), "X-Owner-Tenant-Id": OWNER_B}
    resp = await client.get("/api/settings", headers=headers)
    assert resp.status_code == 403, resp.text
    resp = await client.get("/api/upload/_owner-probe", headers=headers)
    assert resp.status_code == 403, resp.text
    assert config.calls == []


async def test_dual_recorded_cell_token_does_not_elevate_a_customer_licence(
    dual: None,
    client: httpx.AsyncClient,
    config: _RecordingConfig,
    monkeypatch: pytest.MonkeyPatch,
    keys: dict[str, Ed25519PrivateKey],
) -> None:
    """In dual a cell token next to a licence is only RECORDED; the licence lane
    authenticated, so its identity (a customer here) decides, as in w2v_common's
    entitlement gate."""
    customer = "0198f1d2-0000-7000-8000-0000000c5501"
    headers = {**_licence(monkeypatch, customer), **_cell(keys), "X-Owner-Tenant-Id": OWNER_B}
    resp = await client.get("/api/settings", headers=headers)
    assert resp.status_code == 403, resp.text
    assert config.calls == []


async def test_dual_cell_token_alone_takes_the_cell_lane(
    dual: None,
    client: httpx.AsyncClient,
    config: _RecordingConfig,
    keys: dict[str, Ed25519PrivateKey],
) -> None:
    resp = await client.get(
        "/api/upload/_owner-probe", headers={**_cell(keys), "X-Owner-Tenant-Id": OWNER_B}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"channel": OWNER_A, "owner": OWNER_A}
