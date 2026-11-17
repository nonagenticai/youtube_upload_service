"""Unit tests for the ``classify_orphaned_upload_jobs`` operator script.

Pure logic only — fake row dicts, a fake orchestrator run index, NO database and
no network. The script is loaded by path (``scripts/`` is not a package) so the
tests exercise exactly the file an operator runs.

The regressions these lock in:

* ``00000000-0000-0000-0000-000000000001`` is a REAL, ACTIVE tenant (the largest
  one). It must never be classified as broken — treating it as a sentinel once
  locked that tenant out of the upload API entirely.
* Only the **nil** UUID (all zeros) is the "unattributed" sentinel.
* ``file_source`` is corroborating evidence only; a file-only match must land in
  manual review, never in the auto-applied ``RECOVERABLE`` bucket.
* A ``run_id`` that resolves to more than one run is ambiguous, so it is not
  ``RECOVERABLE`` — an ambiguous guess hands one tenant's jobs to another.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "classify_orphaned_upload_jobs.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("classify_orphaned_upload_jobs", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()

CHANNEL_TENANT = "sa-channel-tenant"
REAL_TENANT = "11111111-2222-4333-8444-555555555555"
BIG_REAL_TENANT = "00000000-0000-0000-0000-000000000001"
NIL = "00000000-0000-0000-0000-000000000000"

RUN_A = "018f0000-0000-7000-8000-00000000aaaa"
RUN_B = "018f0000-0000-7000-8000-00000000bbbb"

CAPTION_URL = (
    f"https://minio.test/w2v-artifacts/artifacts/{RUN_A}/soft_cc/{RUN_A}.srt"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=3600"
)
THUMB_URL = f"https://minio.test/w2v-artifacts/artifacts/{RUN_B}/course_thumbnail/cover.png"
FILE_URL = f"https://minio.test/w2v-artifacts/artifacts/{RUN_A}/lesson_compile/master.mp4"


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "job_id": "job-1",
        "tenant_id": CHANNEL_TENANT,
        "owner_tenant_id": None,
        "caption_source": None,
        "thumbnail_source": None,
        "file_source": None,
    }
    row.update(overrides)
    return row


def _runs(*pairs: tuple[str, str | None]):
    return mod.index_runs([mod.RunRef(run_id=r, tenant_id=t) for r, t in pairs])


# ---------------------------------------------------------------------------
# is_broken_owner
# ---------------------------------------------------------------------------


def test_real_lookalike_tenant_0001_is_not_broken() -> None:
    """`…0001` is the largest live tenant — never a sentinel, never broken."""
    assert mod.is_broken_owner(_row(owner_tenant_id=BIG_REAL_TENANT)) is False
    assert mod.is_real_tenant(BIG_REAL_TENANT) is True


def test_nil_uuid_owner_is_broken() -> None:
    assert mod.is_broken_owner(_row(owner_tenant_id=NIL)) is True
    assert mod.is_real_tenant(NIL) is False


def test_owner_equal_to_channel_tenant_is_broken() -> None:
    assert mod.is_broken_owner(_row(owner_tenant_id=CHANNEL_TENANT)) is True


def test_null_and_blank_owner_are_broken() -> None:
    assert mod.is_broken_owner(_row(owner_tenant_id=None)) is True
    assert mod.is_broken_owner(_row(owner_tenant_id="   ")) is True


def test_distinct_real_owner_is_not_broken() -> None:
    assert mod.is_broken_owner(_row(owner_tenant_id=REAL_TENANT)) is False


# ---------------------------------------------------------------------------
# run_id extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        CAPTION_URL,
        f"artifacts/{RUN_A}/soft_cc/{RUN_A}.srt",
        f"file:///data/artifacts/{RUN_A}/soft_cc/{RUN_A}.srt",
        f"/api/runs/{RUN_A}/artifact/w2v-artifacts/x.srt",
    ],
)
def test_extract_run_id_from_supported_key_shapes(source: str) -> None:
    assert mod.extract_run_id(source) == RUN_A


@pytest.mark.parametrize("source", [None, "", "   ", "https://example.com/video.mp4", "not-a-url"])
def test_extract_run_id_returns_none_without_a_marker_anchored_uuid(source: object) -> None:
    assert mod.extract_run_id(source) is None


# ---------------------------------------------------------------------------
# classify_row
# ---------------------------------------------------------------------------


def test_caption_key_with_one_real_run_is_recoverable() -> None:
    verdict = mod.classify_row(_row(caption_source=CAPTION_URL), _runs((RUN_A, REAL_TENANT)))
    assert verdict.classification == mod.RECOVERABLE
    assert verdict.proposed_owner_tenant_id == REAL_TENANT
    assert verdict.evidence_field == "caption_source"
    assert verdict.is_recoverable is True


def test_caption_key_resolving_to_tenant_0001_is_recoverable() -> None:
    """The lookalike tenant is a legitimate recovery target, not a rejection."""
    verdict = mod.classify_row(_row(caption_source=CAPTION_URL), _runs((RUN_A, BIG_REAL_TENANT)))
    assert verdict.classification == mod.RECOVERABLE
    assert verdict.proposed_owner_tenant_id == BIG_REAL_TENANT


def test_caption_is_preferred_over_thumbnail() -> None:
    verdict = mod.classify_row(
        _row(caption_source=CAPTION_URL, thumbnail_source=THUMB_URL),
        _runs((RUN_A, REAL_TENANT), (RUN_B, "other-tenant")),
    )
    assert verdict.evidence_field == "caption_source"
    assert verdict.proposed_owner_tenant_id == REAL_TENANT


def test_thumbnail_only_is_still_a_strong_key() -> None:
    verdict = mod.classify_row(_row(thumbnail_source=THUMB_URL), _runs((RUN_B, REAL_TENANT)))
    assert verdict.classification == mod.RECOVERABLE
    assert verdict.evidence_field == "thumbnail_source"


def test_file_source_only_is_needs_manual_review_weak_key() -> None:
    """A weak key may corroborate but must never be auto-applied."""
    verdict = mod.classify_row(_row(file_source=FILE_URL), _runs((RUN_A, REAL_TENANT)))
    assert verdict.classification == mod.NEEDS_MANUAL_REVIEW_WEAK_KEY
    assert verdict.evidence_field == "file_source"
    assert verdict.proposed_owner_tenant_id is None


def test_run_id_matching_two_runs_is_not_recoverable() -> None:
    verdict = mod.classify_row(
        _row(caption_source=CAPTION_URL),
        _runs((RUN_A, REAL_TENANT), (RUN_A, "second-tenant")),
    )
    assert verdict.classification == mod.NEEDS_MANUAL_REVIEW_WEAK_KEY
    assert verdict.proposed_owner_tenant_id is None
    assert "ambiguous" in verdict.reason


def test_run_id_matching_no_run_is_not_recoverable() -> None:
    verdict = mod.classify_row(_row(caption_source=CAPTION_URL), _runs())
    assert verdict.classification == mod.NEEDS_MANUAL_REVIEW_WEAK_KEY
    assert verdict.proposed_owner_tenant_id is None


def test_run_tenant_equal_to_channel_tenant_is_not_recoverable() -> None:
    verdict = mod.classify_row(_row(caption_source=CAPTION_URL), _runs((RUN_A, CHANNEL_TENANT)))
    assert verdict.classification == mod.NEEDS_MANUAL_REVIEW_WEAK_KEY


def test_run_tenant_nil_uuid_is_not_recoverable() -> None:
    verdict = mod.classify_row(_row(caption_source=CAPTION_URL), _runs((RUN_A, NIL)))
    assert verdict.classification == mod.NEEDS_MANUAL_REVIEW_WEAK_KEY


def test_no_source_key_at_all_is_unrecoverable() -> None:
    verdict = mod.classify_row(
        _row(file_source="https://example.com/plain.mp4"), _runs((RUN_A, REAL_TENANT))
    )
    assert verdict.classification == mod.UNRECOVERABLE_NO_RUN_KEY
    assert verdict.proposed_owner_tenant_id is None


# ---------------------------------------------------------------------------
# classify_rows / apply refusal contract
# ---------------------------------------------------------------------------


def test_classify_rows_skips_rows_with_a_good_owner() -> None:
    rows = [
        _row(job_id="good", owner_tenant_id=REAL_TENANT, caption_source=CAPTION_URL),
        _row(job_id="lookalike", owner_tenant_id=BIG_REAL_TENANT, caption_source=CAPTION_URL),
        _row(job_id="broken", owner_tenant_id=NIL, caption_source=CAPTION_URL),
    ]
    verdicts = mod.classify_rows(rows, _runs((RUN_A, REAL_TENANT)))
    assert [v.job_id for v in verdicts] == ["broken"]


def test_only_recoverable_verdicts_are_writable() -> None:
    """The --apply guard: nothing but RECOVERABLE may ever be written."""
    verdicts = [
        mod.classify_row(_row(caption_source=CAPTION_URL), _runs((RUN_A, REAL_TENANT))),
        mod.classify_row(_row(file_source=FILE_URL), _runs((RUN_A, REAL_TENANT))),
        mod.classify_row(_row(), _runs()),
    ]
    writable = [v for v in verdicts if v.is_recoverable]
    assert len(writable) == 1
    assert writable[0].proposed_owner_tenant_id == REAL_TENANT
    assert all(v.proposed_owner_tenant_id is None for v in verdicts if not v.is_recoverable)


def test_dry_run_is_the_argparse_default() -> None:
    args = mod.build_parser().parse_args([])
    assert args.apply is False
    assert args.dry_run is True


# ---------------------------------------------------------------------------
# Orchestrator lookup vacuity guard (RLS / permissions)
# ---------------------------------------------------------------------------


def test_zero_run_matches_for_a_nonempty_candidate_list_aborts_as_rls_problem() -> None:
    """RLS fails CLOSED to an EMPTY read, so "no matches" must never be believed.

    A restricted orchestrator role (or one missing SELECT on
    deliverables/projects) reads zero rows instead of erroring. Reporting that
    as "every job is UNRECOVERABLE" is a clean-looking, totally wrong result —
    so the guard aborts and names RLS/permissions explicitly.
    """
    with pytest.raises(mod.OrchestratorLookupVacuousError) as excinfo:
        mod.assert_run_lookup_not_vacuous([RUN_A, RUN_B], [])
    message = str(excinfo.value)
    assert "RLS" in message
    assert "permissions" in message
    assert RUN_A in message


def test_no_candidate_run_ids_is_not_an_abort() -> None:
    """Zero candidates legitimately matches zero runs — that is not a failure."""
    mod.assert_run_lookup_not_vacuous([], [])


def test_partial_run_matches_do_not_abort() -> None:
    """One real match proves the role can read runs; the rest are data facts."""
    mod.assert_run_lookup_not_vacuous(
        [RUN_A, RUN_B], [mod.RunRef(run_id=RUN_A, tenant_id=REAL_TENANT)]
    )


def test_orchestrator_query_joins_through_deliverables_to_projects() -> None:
    """`runs` has NO tenant_id: the tenant lives on projects.tenant_id.

    Locked in because the first version of this script did
    ``SELECT id, tenant_id FROM runs``, which is a column that does not exist.
    """
    sql = " ".join(mod._SELECT_RUNS.split()).lower()
    assert "p.tenant_id" in sql
    assert "join deliverables d on d.id = r.deliverable_id" in sql
    assert "join projects p on p.id = d.project_id" in sql
    assert "select r.id, p.tenant_id" in sql
