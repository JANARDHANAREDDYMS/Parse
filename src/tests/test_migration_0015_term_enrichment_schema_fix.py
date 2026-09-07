"""Exercise migration 0015's reconciliation of migration 0013's schema drift.

Mirrors `test_migration_0014_triage_rationale_nullable.py`'s approach:
`env.py`'s online migration path calls `asyncio.run(...)` internally, which
cannot run inside an already-running event loop (every other async test in
this suite uses pytest-asyncio's own loop), so these tests shell out to the
real Alembic CLI as a subprocess. Every test that changes the live schema
restores it to head in a `finally` block.

No Claude call, no PDF, no worker/API process, no mutation of any existing
evaluation record: every row used to test refusal-to-tighten paths belongs
to a freshly generated organization/run created by the test itself and is
cascade-deleted by the `organization` fixture's own teardown.
"""
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from maximor.db.models.term_applicability import (
    CandidateCommercialFactCoverageProjection,
    CandidateCommercialFactProjection,
    TermApplicabilityDecisionProjection,
    TermApplicabilityEvidenceProjection,
    TermTriageDecisionProjection,
)
from maximor.db.session import get_session_factory
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION
from test_sku_mapping_worker_integration import _seed

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_NOT_NULL = {
    "term_triage_decisions": ["run_id", "organization_id", "term_id", "disposition", "source_order"],
    "term_applicability_decisions": ["run_id", "organization_id", "term_id", "disposition", "candidate_ids", "evidence", "source_order"],
    "candidate_commercial_fact_coverages": ["run_id", "organization_id", "candidate_id", "expected_fields", "extracted_fields", "unresolved_fields", "evidence"],
    "candidate_commercial_facts": ["run_id", "organization_id", "candidate_id", "fact_id", "field", "raw_value", "evidence"],
}

EXPECTED_INDEXES = [
    ("term_triage_decisions", "ix_term_triage_decisions_run_id"),
    ("term_triage_decisions", "ix_term_triage_decisions_organization_id"),
    ("term_applicability_decisions", "ix_term_applicability_decisions_run_id"),
    ("term_applicability_decisions", "ix_term_applicability_decisions_organization_id"),
    ("candidate_commercial_fact_coverages", "ix_candidate_commercial_fact_coverages_run_id"),
    ("candidate_commercial_fact_coverages", "ix_candidate_commercial_fact_coverages_organization_id"),
    ("candidate_commercial_facts", "ix_candidate_commercial_facts_run_id"),
    ("candidate_commercial_facts", "ix_candidate_commercial_facts_organization_id"),
    ("term_applicability_evidence_references", "ix_term_applicability_evidence_references_run_id"),
    ("term_applicability_evidence_references", "ix_term_applicability_evidence_references_organization_id"),
    ("term_applicability_runs", "ix_term_applicability_runs_analysis_run_id"),
    ("term_applicability_runs", "ix_term_applicability_runs_document_id"),
    ("term_applicability_runs", "ix_term_applicability_runs_organization_id"),
    ("term_applicability_runs", "ix_term_applicability_runs_processing_job_id"),
]

EXPECTED_FKS = [
    ("term_triage_decisions", "fk_term_triage_decisions_organization_id_organizations"),
    ("term_applicability_decisions", "fk_term_applicability_decisions_organization_id_organizations"),
    ("candidate_commercial_fact_coverages", "fk_candidate_fact_coverage_org"),
    ("candidate_commercial_facts", "fk_candidate_commercial_facts_organization_id_organizations"),
    ("term_applicability_evidence_references", "fk_term_evidence_refs_org"),
]


def _alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )


async def _column_nullable(table: str, column: str) -> bool:
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            "select is_nullable from information_schema.columns where table_name=:t and column_name=:c"
        ), {"t": table, "c": column})).one()
    return row[0] == "YES"


async def _column_exists(table: str, column: str) -> bool:
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            "select count(*) from information_schema.columns where table_name=:t and column_name=:c"
        ), {"t": table, "c": column})).scalar()
    return row > 0


async def _index_exists(table: str, index_name: str) -> bool:
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            "select count(*) from pg_indexes where tablename=:t and indexname=:i"
        ), {"t": table, "i": index_name})).scalar()
    return row > 0


async def _fk_exists(table: str, fk_name: str) -> bool:
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            "select count(*) from information_schema.table_constraints "
            "where table_name=:t and constraint_name=:c and constraint_type='FOREIGN KEY'"
        ), {"t": table, "c": fk_name})).scalar()
    return row > 0


def test_migration_0015_is_registered_directly_after_0014():
    result = _alembic("history")
    assert result.returncode == 0, result.stderr
    assert "0014_triage_rationale_nullable -> 0015_term_enrichment_schema_fix" in result.stdout
    assert len("0015_term_enrichment_schema_fix") <= 32


@pytest.mark.asyncio
async def test_upgrade_tightens_every_required_column_and_loosens_raw_period_label():
    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    for table, columns in REQUIRED_NOT_NULL.items():
        for column in columns:
            assert not await _column_nullable(table, column), f"{table}.{column} should be NOT NULL"

    assert await _column_nullable("candidate_commercial_facts", "raw_period_label")
    assert await _column_nullable("term_applicability_decisions", "applicability_scope")  # unaffected, stays optional
    assert await _column_nullable("term_triage_decisions", "rationale")  # unaffected, fixed by 0014


@pytest.mark.asyncio
async def test_upgrade_drops_the_two_columns_absent_from_the_orm_model():
    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert not await _column_exists("candidate_commercial_fact_coverages", "source_order")
    assert not await _column_exists("candidate_commercial_facts", "source_order")


@pytest.mark.asyncio
async def test_upgrade_adds_every_missing_index_and_foreign_key():
    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    for table, index_name in EXPECTED_INDEXES:
        assert await _index_exists(table, index_name), f"missing index {index_name} on {table}"
    for table, fk_name in EXPECTED_FKS:
        assert await _fk_exists(table, fk_name), f"missing foreign key {fk_name} on {table}"


@pytest.mark.asyncio
async def test_alembic_check_reports_no_term_enrichment_drift_at_head():
    """`alembic check` reports zero drift for any term-enrichment table.

    A pre-existing, unrelated `sku_mapping_decisions` missing-index item
    (introduced long before migration 0013 and never touched by it) may
    still appear; this asserts only that no *term-enrichment* table is
    named in whatever `alembic check` reports.
    """

    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    result = _alembic("check")
    combined = result.stdout + result.stderr
    term_enrichment_tables = (
        "term_triage_decisions", "term_applicability_decisions",
        "candidate_commercial_fact_coverages", "candidate_commercial_facts",
        "term_applicability_evidence_references", "term_applicability_runs",
    )
    for table in term_enrichment_tables:
        assert table not in combined, f"unexpected remaining drift mentions {table}: {combined[:2000]}"


@pytest.mark.asyncio
async def test_downgrade_succeeds_when_no_null_rows_and_restores_prior_shape():
    _alembic("upgrade", "head")
    async with get_session_factory()() as session:
        count = (await session.execute(text(
            "select count(*) from candidate_commercial_facts where raw_period_label is null"
        ))).scalar()
    assert count == 0, "precondition: no pre-existing null raw_period_label rows"

    try:
        result = _alembic("downgrade", "-1")
        assert result.returncode == 0, result.stderr
        assert await _column_nullable("candidate_commercial_fact_coverages", "run_id")
        assert not await _column_nullable("candidate_commercial_facts", "raw_period_label")
        assert await _column_exists("candidate_commercial_fact_coverages", "source_order")
        assert not await _fk_exists("candidate_commercial_fact_coverages", "fk_candidate_fact_coverage_org")
    finally:
        restore = _alembic("upgrade", "head")
        assert restore.returncode == 0, restore.stderr
    assert not await _column_nullable("candidate_commercial_fact_coverages", "run_id")


@pytest.mark.asyncio
async def test_downgrade_refuses_without_rewriting_data_when_null_raw_period_label_exists(tmp_path, organization):
    """Downgrading must not silently discard a null `raw_period_label` by forcing it NOT NULL."""

    _alembic("upgrade", "head")
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    enrichment = TermApplicabilityPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = uuid.uuid4()
    await enrichment.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1,
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, prompt_version="p",
        skill_version="s", agent_version="a", model="m", started_at=datetime.now(UTC),
    )
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(CandidateCommercialFactProjection(
                run_id=run_id, organization_id=organization, candidate_id="candidate-x",
                fact_id="fact:candidate-x:quantity", field="quantity", raw_value="10",
                raw_period_label=None, evidence=[],
            ))

    try:
        result = _alembic("downgrade", "-1")
        assert result.returncode != 0
        assert "raw_period_label" in (result.stdout + result.stderr)
        assert await _column_nullable("candidate_commercial_facts", "raw_period_label")
    finally:
        restore = _alembic("upgrade", "head")
        assert restore.returncode == 0, restore.stderr

    async with get_session_factory()() as session:
        row = await session.get(CandidateCommercialFactProjection, (await session.execute(
            text("select id from candidate_commercial_facts where run_id = :run_id"), {"run_id": run_id},
        )).scalar())
    assert row.raw_period_label is None


@pytest.mark.asyncio
async def test_upgrade_refuses_without_rewriting_data_when_a_required_column_has_a_null(tmp_path, organization):
    """A hypothetical null in a to-be-tightened column aborts upgrade cleanly, never rewritten.

    Constructs the null directly via a raw SQL UPDATE (bypassing the ORM's
    own NOT NULL-respecting model, which cannot represent this state) after
    first downgrading past 0015 so the column is still nullable enough to
    accept it.
    """

    _alembic("upgrade", "head")
    _alembic("downgrade", "-1")  # back to 0014: term_id etc. are nullable again
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    enrichment = TermApplicabilityPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = uuid.uuid4()
    await enrichment.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1,
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, prompt_version="p",
        skill_version="s", agent_version="a", model="m", started_at=datetime.now(UTC),
    )
    async with get_session_factory()() as session:
        async with session.begin():
            session.add(TermTriageDecisionProjection(
                run_id=run_id, organization_id=organization, term_id="term-y",
                disposition="uncertain", source_order=0, rationale=None,
            ))
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(text(
                "update term_triage_decisions set disposition = NULL where run_id = :run_id"
            ), {"run_id": run_id})

    try:
        result = _alembic("upgrade", "head")
        assert result.returncode != 0
        assert "disposition" in (result.stdout + result.stderr)
        assert await _column_nullable("term_triage_decisions", "disposition")
    finally:
        # Clean up the row this test injected before restoring head, so the
        # normal (no-null) upgrade used by every other test in this file
        # succeeds again.
        async with get_session_factory()() as session:
            async with session.begin():
                await session.execute(text("delete from term_triage_decisions where run_id = :run_id"), {"run_id": run_id})
        restore = _alembic("upgrade", "head")
        assert restore.returncode == 0, restore.stderr
