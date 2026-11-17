#!/usr/bin/env python3
"""Operator tool: classify (and optionally repair) ``upload_jobs`` rows whose
``owner_tenant_id`` is wrong.

READ-ONLY BY DEFAULT. This script reports and changes NOTHING unless you pass
``--apply``. It is an operator tool for a human to run by hand against a
specific database, review the printed classification, and only then decide what
to backfill. It is not wired into the service, CI, or any migration.

Why rows are broken
-------------------
``upload_jobs.tenant_id`` is the YouTube *channel*'s service-account tenant. The
orchestrator authenticates with that same SA token for every caller, so
``tenant_id`` is IDENTICAL for all rows and is NEVER a valid owner signal.
``owner_tenant_id`` is the real caller. A row is broken when
``owner_tenant_id``:

* IS NULL (created before migration ``005_add_owner_tenant_id``), or
* equals ``tenant_id`` (the channel tenant was mistakenly copied in), or
* is the nil UUID ``00000000-0000-0000-0000-000000000000`` — the deliberate
  "unattributed" fallback.

⚠️ ``00000000-0000-0000-0000-000000000001`` is a REAL, ACTIVE tenant (the
largest one). It is NOT a sentinel and is NEVER treated as broken.

Recovery is a TWO-STEP, TWO-DATABASE lookup
-------------------------------------------
The recovery key is the object key embedded in ``caption_source`` (it carries
the orchestrator ``run_id`` as ``artifacts/<run_id>/<stage>/<file>``);
``thumbnail_source`` is a weaker second choice. ``file_source`` is
weak/corroborating ONLY and is never auto-applied.

Step 1 (upload DB): extract candidate ``run_id`` values from the broken rows.
Step 2 (orchestrator DB): resolve those run ids to a tenant.

⚠️ ``runs`` has **NO** ``tenant_id`` column. The tenant is reached by climbing
two NOT NULL FKs (verified against ``w2v_orchestrator``
``origin/main:src/database/models.py``)::

    runs.deliverable_id      -> deliverables.id    (Run.deliverable_id,    :384)
    deliverables.project_id  -> projects.id        (Deliverable.project_id,:323)
    projects.tenant_id                             (Project.tenant_id,     :248)

so step 2 is::

    SELECT r.id, p.tenant_id
      FROM runs r
      JOIN deliverables d ON d.id = r.deliverable_id
      JOIN projects p ON p.id = d.project_id
     WHERE r.id = ANY(%(run_ids)s)

⛔ The orchestrator connection MUST be a role that can see **every** tenant's
runs (the table owner, or a ``BYPASSRLS``/``SECURITY DEFINER`` role) and that
holds ``SELECT`` on all three tables. The orchestrator carries tenant RLS of its
own, and RLS fails **closed to an EMPTY result, not to an error** — a restricted
role without ``app.current_tenant`` bound reads zero rows and every job would be
silently reported ``UNRECOVERABLE_NO_RUN_KEY`` in a report that looks clean.
:func:`assert_run_lookup_not_vacuous` refuses that case: a non-empty candidate
list that matches zero runs ABORTS (exit 3) instead of being reported.

The two databases are SEPARATE. They are never joined in one statement, and the
script opens two independent connections.

Classification
--------------
``RECOVERABLE``
    A strong key (caption, else thumbnail) yields a ``run_id`` that resolves to
    EXACTLY ONE run whose tenant is real (non-empty, not the nil UUID) and
    differs from the channel ``tenant_id``.
``NEEDS_MANUAL_REVIEW_WEAK_KEY``
    Everything ambiguous: only ``file_source`` carried a run key; or the strong
    key resolved to zero runs, to more than one run, or to a tenant that is nil
    / empty / equal to the channel tenant. A human must decide.
``UNRECOVERABLE_NO_RUN_KEY``
    No source column contains an extractable ``run_id`` at all.

⛔ UNRECOVERABLE rows must be LEFT NULL rather than guessed. An invented owner
silently grants one tenant's jobs to another; a NULL owner merely hides the row
from list/get, which is the safe failure. ``--apply`` refuses to touch anything
that is not ``RECOVERABLE``.

Connections
-----------
Both DSNs come from the environment (or CLI args) — no credential is ever
hardcoded here:

* upload DB: ``DATABASE_URL`` (a SQLAlchemy ``postgresql+psycopg://`` URL is
  accepted and normalised).
* orchestrator DB: ``--orchestrator-dsn`` or ``ORCHESTRATOR_DATABASE_URL``.

RLS
---
Migration ``004_rls`` is ``ENABLE`` (not ``FORCE``) on ``tenant_id`` — not on
``owner_tenant_id``. The table owner role bypasses it entirely, which is the
expected way to run this tool. If you must run as the restricted app role, pass
``--channel-tenant <channel tenant id>``: the script then issues
``SELECT set_config('app.current_tenant', <channel tenant>, true)`` INSIDE the
transaction (transaction-local, per the ``SECURITY DEFINER`` pattern in
``src/database/rls_resolvers.py``); without it a cross-tenant read fails closed
to zero rows.

psycopg 3 only. No asyncpg, no SQLite.

Usage
-----
    export DATABASE_URL=...            # upload DB
    export ORCHESTRATOR_DATABASE_URL=...
    uv run python scripts/classify_orphaned_upload_jobs.py            # dry run
    uv run python scripts/classify_orphaned_upload_jobs.py --apply    # writes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from itertools import pairwise
from urllib.parse import unquote, urlsplit

import psycopg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The deliberate "unattributed" fallback written when no caller tenant was
#: known. A row carrying this is broken.
NIL_UUID = "00000000-0000-0000-0000-000000000000"

#: Values that mean "no real caller tenant was bound" in the HISTORICAL rows this
#: script classifies. This is deliberately a string set and no longer mirrors a
#: runtime guard: the live boundary is now ``src.tenant.require_tenant``, which
#: refuses anything that is not a real non-nil UUID, so these slugs can no longer
#: be written at all. What is already in the table still has to be recognised, and
#: recognising it needs the exact legacy spellings — hence the set stays.
#: ``…0001`` is NOT in it (it is a real, active tenant).
UNBOUND_OWNER_VALUES = frozenset({"anonymous", "none", "null", NIL_UUID})

#: NOT a sentinel — a real, active, large tenant. Kept as a named constant so
#: nobody "helpfully" adds it to the broken set. Never referenced by the broken
#: predicate on purpose.
REAL_TENANT_LOOKALIKE = "00000000-0000-0000-0000-000000000001"

RECOVERABLE = "RECOVERABLE"
NEEDS_MANUAL_REVIEW_WEAK_KEY = "NEEDS_MANUAL_REVIEW_WEAK_KEY"
UNRECOVERABLE_NO_RUN_KEY = "UNRECOVERABLE_NO_RUN_KEY"

#: Source columns in recovery-strength order. ``strong`` keys may be applied
#: automatically; a weak key can only ever reach manual review.
STRONG_KEY_FIELDS: tuple[str, ...] = ("caption_source", "thumbnail_source")
WEAK_KEY_FIELDS: tuple[str, ...] = ("file_source",)

#: Path segments that introduce a run id: ``artifacts/<run_id>/<stage>/<file>``
#: (``StorageService.store_stage_artifact``) and ``/api/runs/<run_id>/artifact/``
#: (the orchestrator artifact proxy).
_RUN_ID_MARKERS = frozenset({"artifacts", "runs"})

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRef:
    """One row of the orchestrator's ``runs`` table (the step-2 lookup)."""

    run_id: str
    tenant_id: str | None


@dataclass(frozen=True)
class Classified:
    """The verdict for one ``upload_jobs`` row. Pure data — safe to print."""

    job_id: str
    classification: str
    reason: str
    channel_tenant_id: str | None
    current_owner_tenant_id: str | None
    proposed_owner_tenant_id: str | None = None
    evidence_field: str | None = None
    run_id: str | None = None
    matched_run_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_recoverable(self) -> bool:
        return self.classification == RECOVERABLE


# ---------------------------------------------------------------------------
# Pure classification logic (unit-tested; no DB)
# ---------------------------------------------------------------------------


def _clean(value: object) -> str | None:
    """Normalise a possibly-NULL text column to a stripped str or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_real_tenant(tenant_id: object) -> bool:
    """True when ``tenant_id`` is a usable owner value.

    Only empty/whitespace and the unbound sentinels (nil UUID, ``anonymous``,
    ``none``, ``null``) are rejected. ``…0001`` is a REAL tenant and passes.
    """
    cleaned = _clean(tenant_id)
    return cleaned is not None and cleaned.lower() not in UNBOUND_OWNER_VALUES


def is_broken_owner(row: Mapping[str, object]) -> bool:
    """True when this row's ``owner_tenant_id`` cannot be trusted.

    Broken == NULL/empty, equal to the channel ``tenant_id``, or one of the
    unbound sentinels (nil UUID / ``anonymous`` / ``none`` / ``null``).
    ``…0001`` is never broken on the strength of its value alone.
    """
    owner = _clean(row.get("owner_tenant_id"))
    if owner is None:
        return True
    if owner.lower() in UNBOUND_OWNER_VALUES:
        return True
    channel = _clean(row.get("tenant_id"))
    return channel is not None and owner == channel


def extract_run_id(source: object) -> str | None:
    """Pull the orchestrator ``run_id`` out of an artifact URL / object key.

    Recognises ``…/artifacts/<run_id>/<stage>/<file>`` and
    ``/api/runs/<run_id>/artifact/…`` in ``http(s)://``, ``file://`` and bare
    key forms. Query strings (presign params) are ignored. Returns ``None``
    when no marker-anchored UUID segment is present — deliberately conservative:
    a guessed run id becomes a guessed owner.
    """
    raw = _clean(source)
    if raw is None:
        return None
    path = unquote(urlsplit(raw).path or raw)
    segments = [seg for seg in path.split("/") if seg]
    for marker, nxt in pairwise(segments):
        if marker.lower() in _RUN_ID_MARKERS and _UUID_RE.match(nxt):
            return nxt.lower()
    return None


def candidate_run_keys(row: Mapping[str, object]) -> list[tuple[str, str, bool]]:
    """``(field_name, run_id, is_strong)`` candidates, strongest first.

    Order is ``caption_source`` → ``thumbnail_source`` → ``file_source``.
    ``file_source`` is always returned as weak, so it can corroborate but never
    auto-apply.
    """
    found: list[tuple[str, str, bool]] = []
    for name in STRONG_KEY_FIELDS:
        run_id = extract_run_id(row.get(name))
        if run_id:
            found.append((name, run_id, True))
    for name in WEAK_KEY_FIELDS:
        run_id = extract_run_id(row.get(name))
        if run_id:
            found.append((name, run_id, False))
    return found


def classify_row(
    row: Mapping[str, object],
    runs_by_id: Mapping[str, Sequence[RunRef]],
) -> Classified:
    """Classify one broken row against the orchestrator run lookup.

    ``runs_by_id`` maps a ``run_id`` to EVERY run row that matched it. More than
    one match is ambiguous and is never ``RECOVERABLE``.
    """
    job_id = str(row.get("job_id") or "")
    channel = _clean(row.get("tenant_id"))
    owner = _clean(row.get("owner_tenant_id"))
    base = Classified(
        job_id=job_id,
        classification=UNRECOVERABLE_NO_RUN_KEY,
        reason="",
        channel_tenant_id=channel,
        current_owner_tenant_id=owner,
    )

    candidates = candidate_run_keys(row)
    if not candidates:
        return replace(
            base,
            reason="no run_id could be extracted from caption/thumbnail/file source",
        )

    strong = [c for c in candidates if c[2]]
    if not strong:
        name, run_id, _ = candidates[0]
        return replace(
            base,
            classification=NEEDS_MANUAL_REVIEW_WEAK_KEY,
            reason=f"only the weak key {name} carried a run_id; never auto-applied",
            evidence_field=name,
            run_id=run_id,
        )

    name, run_id, _ = strong[0]
    matches = list(runs_by_id.get(run_id) or ())
    found = replace(
        base,
        classification=NEEDS_MANUAL_REVIEW_WEAK_KEY,
        evidence_field=name,
        run_id=run_id,
        matched_run_ids=tuple(m.run_id for m in matches),
    )

    if not matches:
        return replace(
            found,
            reason=f"{name} run_id {run_id} matched no row in the orchestrator runs table",
        )
    if len(matches) > 1:
        return replace(
            found,
            reason=f"{name} run_id {run_id} matched {len(matches)} runs - ambiguous owner",
        )

    run_tenant = _clean(matches[0].tenant_id)
    if not is_real_tenant(run_tenant):
        return replace(found, reason=f"run {run_id} has no real tenant (got {run_tenant!r})")
    if channel is not None and run_tenant == channel:
        return replace(
            found,
            reason=(
                f"run {run_id} tenant equals the channel tenant {channel} - "
                "indistinguishable from the broken value"
            ),
        )

    return replace(
        found,
        classification=RECOVERABLE,
        reason=f"{name} run_id {run_id} resolves to exactly one run owned by {run_tenant}",
        proposed_owner_tenant_id=run_tenant,
    )


class OrchestratorLookupVacuousError(RuntimeError):
    """Raised when the orchestrator run lookup returned NOTHING for real inputs.

    RLS on the orchestrator side fails CLOSED to zero rows, not to an error. A
    silent empty result would classify every single row ``UNRECOVERABLE`` and
    print a clean-looking report, so it is treated as an operator error instead.
    """


def assert_run_lookup_not_vacuous(
    run_ids: Sequence[str],
    runs: Sequence[RunRef],
) -> None:
    """Abort loudly when a NON-EMPTY candidate list matched ZERO runs.

    ⛔ Do not "handle" this by reporting the rows as unrecoverable. Zero matches
    for tens of real, well-formed run ids is not a data fact — it is almost
    always the orchestrator connection running as a role that cannot see other
    tenants' runs (tenant RLS reads EMPTY rather than raising), or a role
    lacking ``SELECT`` on ``deliverables``/``projects``.
    """
    if run_ids and not runs:
        raise OrchestratorLookupVacuousError(
            f"orchestrator lookup returned 0 rows for {len(run_ids)} candidate run id(s) "
            "(first: "
            f"{run_ids[0]}). This is almost certainly an RLS/permissions problem, NOT "
            "proof that the runs are gone: the orchestrator's tenant RLS fails CLOSED to "
            "an EMPTY result instead of erroring, and the join climbs "
            "runs -> deliverables -> projects.tenant_id, so SELECT is needed on all "
            "three. Re-run --orchestrator-dsn as a role that can see EVERY tenant's runs "
            "(the table owner, or a SECURITY DEFINER/BYPASSRLS role). Refusing to report "
            "these rows as UNRECOVERABLE, which is what a silent empty read would look "
            "like."
        )


def classify_rows(
    rows: Iterable[Mapping[str, object]],
    runs_by_id: Mapping[str, Sequence[RunRef]],
) -> list[Classified]:
    """Classify every broken row (non-broken rows are skipped, not reported)."""
    return [classify_row(row, runs_by_id) for row in rows if is_broken_owner(row)]


def index_runs(runs: Iterable[RunRef]) -> dict[str, list[RunRef]]:
    """Group orchestrator run rows by id so duplicates stay visible."""
    grouped: dict[str, list[RunRef]] = {}
    for run in runs:
        grouped.setdefault(run.run_id.lower(), []).append(run)
    return grouped


# ---------------------------------------------------------------------------
# Database access (psycopg 3). Never invoked by the unit tests.
# ---------------------------------------------------------------------------

#: Column order of ``_SELECT_BROKEN`` — the two MUST stay aligned.
_BROKEN_COLUMNS: tuple[str, ...] = (
    "job_id",
    "tenant_id",
    "owner_tenant_id",
    "caption_source",
    "thumbnail_source",
    "file_source",
    "status",
    "created_at",
)

_SELECT_BROKEN = """
SELECT job_id,
       tenant_id,
       owner_tenant_id,
       caption_source,
       thumbnail_source,
       file_source,
       status,
       created_at
  FROM upload_jobs
 WHERE owner_tenant_id IS NULL
    OR btrim(owner_tenant_id) = ''
    OR owner_tenant_id = tenant_id
    OR lower(owner_tenant_id) = ANY(%(unbound)s)
 ORDER BY created_at
"""

#: ⚠️ ``runs`` has NO ``tenant_id`` column. Verified against
#: ``w2v_orchestrator`` ``origin/main:src/database/models.py``: ``Run.deliverable_id``
#: (models.py:384) → ``Deliverable.project_id`` (models.py:323) →
#: ``Project.tenant_id`` (models.py:248), which is the ONLY tenant column in the
#: chain. Both joins are on NOT NULL FKs, so an inner join loses nothing.
_SELECT_RUNS = """
SELECT r.id, p.tenant_id
  FROM runs r
  JOIN deliverables d ON d.id = r.deliverable_id
  JOIN projects p ON p.id = d.project_id
 WHERE r.id = ANY(%(run_ids)s)
"""

_UPDATE_OWNER = """
UPDATE upload_jobs
   SET owner_tenant_id = %(owner)s
 WHERE job_id = %(job_id)s
   AND (owner_tenant_id IS NULL OR owner_tenant_id = %(expected_owner)s)
RETURNING owner_tenant_id
"""

_SET_TENANT_GUC = "SELECT set_config('app.current_tenant', %(tenant)s, true)"


def normalize_dsn(url: str) -> str:
    """Strip a SQLAlchemy driver suffix so psycopg accepts the URL as-is."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgres+psycopg://", "postgres://", 1
    )


def _bind_tenant(cur: psycopg.Cursor, channel_tenant: str | None) -> None:
    """Bind the transaction-local RLS GUC when running as the restricted role."""
    if channel_tenant:
        cur.execute(_SET_TENANT_GUC, {"tenant": channel_tenant})


def fetch_broken_rows(dsn: str, channel_tenant: str | None) -> list[dict[str, object]]:
    """Read candidate rows from the UPLOAD database (read-only).

    Tuple rows zipped against :data:`_BROKEN_COLUMNS` — the column order in
    ``_SELECT_BROKEN`` is the contract, so keep the two lists aligned.
    """
    with psycopg.connect(normalize_dsn(dsn)) as conn, conn.cursor() as cur:
        _bind_tenant(cur, channel_tenant)
        cur.execute(_SELECT_BROKEN, {"unbound": sorted(UNBOUND_OWNER_VALUES)})
        return [dict(zip(_BROKEN_COLUMNS, row, strict=False)) for row in cur.fetchall()]


def fetch_runs(dsn: str, run_ids: Sequence[str]) -> list[RunRef]:
    """Read ``runs`` from the ORCHESTRATOR database — a SEPARATE connection.

    Never joined with ``upload_jobs``: they live in different databases.
    """
    if not run_ids:
        return []
    with psycopg.connect(normalize_dsn(dsn)) as conn, conn.cursor() as cur:
        cur.execute(_SELECT_RUNS, {"run_ids": list(run_ids)})
        return [
            RunRef(run_id=str(row[0]), tenant_id=None if row[1] is None else str(row[1]))
            for row in cur.fetchall()
        ]


def apply_recoverable(
    dsn: str,
    verdicts: Sequence[Classified],
    channel_tenant: str | None,
) -> int:
    """Write ONLY ``RECOVERABLE`` rows, in one transaction, printing before/after.

    Anything else is refused loudly. A row whose owner changed underneath us
    (the ``expected_owner`` guard) is skipped rather than overwritten.
    """
    recoverable = [v for v in verdicts if v.is_recoverable]
    refused = [v for v in verdicts if not v.is_recoverable]
    for v in refused:
        print(f"  REFUSED  {v.job_id}  {v.classification}: {v.reason}")
    if not recoverable:
        print("nothing to apply — 0 RECOVERABLE rows")
        return 0

    updated = 0
    with psycopg.connect(normalize_dsn(dsn)) as conn:
        with conn.cursor() as cur:
            _bind_tenant(cur, channel_tenant)
            for v in recoverable:
                cur.execute(
                    _UPDATE_OWNER,
                    {
                        "owner": v.proposed_owner_tenant_id,
                        "job_id": v.job_id,
                        "expected_owner": v.current_owner_tenant_id,
                    },
                )
                got = cur.fetchone()
                if got is None:
                    print(f"  SKIP     {v.job_id}  owner changed since classification")
                    continue
                print(
                    f"  UPDATE   {v.job_id}  "
                    f"before={v.current_owner_tenant_id!r} after={got[0]!r}  "
                    f"(via {v.evidence_field} run {v.run_id})"
                )
                updated += 1
        conn.commit()
    return updated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _report(verdicts: Sequence[Classified], as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(v) for v in verdicts], indent=2, default=str))
        return
    for v in verdicts:
        print(f"{v.classification:<30} {v.job_id}")
        print(f"  owner now : {v.current_owner_tenant_id!r}  (channel {v.channel_tenant_id!r})")
        if v.proposed_owner_tenant_id:
            print(f"  proposed  : {v.proposed_owner_tenant_id!r}")
        print(f"  why       : {v.reason}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="classify_orphaned_upload_jobs",
        description=(
            "Classify upload_jobs rows with a bad owner_tenant_id. "
            "READ-ONLY unless --apply is given."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="report only (default; implied whenever --apply is absent)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write owner_tenant_id for RECOVERABLE rows ONLY, in one transaction",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="upload DB DSN (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--orchestrator-dsn",
        default=os.environ.get("ORCHESTRATOR_DATABASE_URL"),
        help="orchestrator DB DSN (default: $ORCHESTRATOR_DATABASE_URL)",
    )
    parser.add_argument(
        "--channel-tenant",
        default=None,
        help=(
            "bind app.current_tenant to this channel tenant inside the transaction; "
            "required only when connecting as the restricted app role (RLS ENABLE)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.database_url:
        print("DATABASE_URL is not set (or --database-url not given)", file=sys.stderr)
        return 2
    if not args.orchestrator_dsn:
        print(
            "ORCHESTRATOR_DATABASE_URL is not set (or --orchestrator-dsn not given)",
            file=sys.stderr,
        )
        return 2

    rows = fetch_broken_rows(args.database_url, args.channel_tenant)
    broken = [r for r in rows if is_broken_owner(r)]
    run_ids = sorted({rid for r in broken for _, rid, _ in candidate_run_keys(r)})
    runs = fetch_runs(args.orchestrator_dsn, run_ids)
    try:
        assert_run_lookup_not_vacuous(run_ids, runs)
    except OrchestratorLookupVacuousError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 3
    runs_by_id = index_runs(runs)
    verdicts = classify_rows(broken, runs_by_id)

    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.classification] = counts.get(v.classification, 0) + 1
    _report(verdicts, args.json)
    print(
        f"\n{len(verdicts)} broken rows: "
        + ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))
    )

    if not args.apply:
        print("DRY RUN — nothing was written. Re-run with --apply to fix RECOVERABLE rows.")
        return 0

    print("\n--apply: writing RECOVERABLE rows only")
    updated = apply_recoverable(args.database_url, verdicts, args.channel_tenant)
    print(
        f"applied {updated} update(s). "
        "UNRECOVERABLE and NEEDS_MANUAL_REVIEW rows were left untouched (NULL stays NULL)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
