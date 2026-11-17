"""Test helpers for the orchestrator service-account lane (``X-Owner-Tenant-Id``).

``src.api.dependencies.caller_is_orchestrator_sa`` honours the owner header only
when w2v_common's licence lane ran (``request.state.license`` = the introspection
result) AND its ``subject_id`` is in ``AUTH_DELEGATING_SUBJECTS``. These helpers
reproduce exactly that state, so the REAL dependency runs in route tests.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

__all__ = ["SA_SUBJECT", "allow_orchestrator_sa", "sa_introspection", "stamp_license_lane"]

#: The orchestrator SA's introspected subject in tests.
SA_SUBJECT = "0198a5e2-5a5a-7000-8000-00000000a5a5"


def sa_introspection(subject: str = SA_SUBJECT) -> dict[str, object]:
    """What the licence lane stores on ``request.state.license``."""
    return {"active": True, "subject_id": subject, "tenant_id": "irrelevant-here"}


def allow_orchestrator_sa(monkeypatch: pytest.MonkeyPatch, subject: str = SA_SUBJECT) -> None:
    """Configure ``AUTH_DELEGATING_SUBJECTS`` (the autouse fixture clears ``AUTH_*``)."""
    monkeypatch.setenv("AUTH_DELEGATING_SUBJECTS", subject)


def stamp_license_lane(app: FastAPI, subject: str | None = SA_SUBJECT) -> None:
    """Stamp ``request.state.license`` as the licence lane does; ``None`` = JWT lane."""

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.license = sa_introspection(subject) if subject is not None else None
        return await call_next(request)
