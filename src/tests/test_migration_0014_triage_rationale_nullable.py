"""Exercise migration 0014's upgrade/downgrade safety for term_triage_decisions.rationale.

`env.py`'s online migration path calls `asyncio.run(...)` internally, which
cannot be invoked from inside an already-running event loop -- and every
other async test in this suite runs under pytest-asyncio's own loop. These
tests instead shell out to the real Alembic CLI as a subprocess (its own
process, its own loop), exactly how this migration was manually verified.
Every test that changes the live schema restores it to head in a `finally`
block, so no other test's assumption about the current schema is ever left
broken by a failure here.

No Claude call, no PDF, no worker/API process, no mutation of any existing
evaluation record: the one null-rationale row used to test the downgrade
refusal path belongs to a freshly generated organization/run created by
this test and is cascade-deleted by the `organization` fixture's own
teardown.
"""
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from maximor.db.models.term_applicability import TermTriageDecisionProjection
from maximor.db.session import get_session_factory
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION
from test_sku_mapping_worker_integration import _seed

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic(*args: str) -> subprocess.CompletedProcess:
    """Run the real Alembic CLI as a subprocess against the shared integration database."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )


async def _rationale_is_nullable() -> bool:
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            "select is_nullable from information_schema.columns "
            "where table_name='term_triage_decisions' and column_name='rationale'"
        ))).one()
    return row[0] == "YES"


def test_migration_0014_is_registered_directly_after_0013():
    """The revision chain links up, and the revision id fits `alembic_version`'s 32-char column."""

    result = _alembic("history")
    assert result.returncode == 0, result.stderr
    assert "0013_term_enrichment_persistence -> 0014_triage_rationale_nullable" in result.stdout
    assert len("0014_triage_rationale_nullable") <= 32


@pytest.mark.asyncio
async def test_upgrade_to_head_makes_rationale_nullable_with_no_column_level_drift():
    """Upgrading to head relaxes the column, matching the ORM model's own declared nullability.

    Scoped to this one column deliberately: `alembic check` reports far
    broader, pre-existing drift across sibling tables from migration 0013's
    unrelated index/foreign-key/nullable-inversion issues (affecting
    `term_applicability_decisions`, `candidate_commercial_facts`, and
    `candidate_commercial_fact_coverages` too) -- confirmed but explicitly
    out of scope for this fix, so it cannot be used as a whole-schema oracle
    here without conflating unrelated, pre-existing problems with this one.
    """

    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr
    assert await _rationale_is_nullable()
    assert TermTriageDecisionProjection.__table__.c.rationale.nullable is True


@pytest.mark.asyncio
async def test_downgrade_succeeds_when_no_null_rationale_rows_exist():
    _alembic("upgrade", "head")
    async with get_session_factory()() as session:
        count = (await session.execute(text(
            "select count(*) from term_triage_decisions where rationale is null"
        ))).scalar()
    assert count == 0, "precondition: no pre-existing null-rationale rows in the shared database"

    try:
        # Target this migration's own down_revision explicitly: relative
        # `-1` would downgrade whatever is currently at the tip of the
        # chain (e.g. migration 0015, once it exists), not 0014 itself.
        result = _alembic("downgrade", "0013_term_enrichment_persistence")
        assert result.returncode == 0, result.stderr
        assert not await _rationale_is_nullable()
    finally:
        restore = _alembic("upgrade", "head")
        assert restore.returncode == 0, restore.stderr
    assert await _rationale_is_nullable()


@pytest.mark.asyncio
async def test_downgrade_refuses_without_rewriting_data_when_a_null_rationale_row_exists(tmp_path, organization):
    """A safe abort, never a silent NULL-to-empty-string rewrite, when downgrading would lose data."""

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
            session.add(TermTriageDecisionProjection(
                run_id=run_id, organization_id=organization, term_id="term-null-rationale",
                disposition="uncertain", source_order=0, rationale=None,
            ))

    try:
        result = _alembic("downgrade", "0013_term_enrichment_persistence")
        assert result.returncode != 0
        assert "NULL rationale" in (result.stdout + result.stderr)
        # The abort must leave the column exactly as it was -- still nullable.
        assert await _rationale_is_nullable()
    finally:
        restore = _alembic("upgrade", "head")
        assert restore.returncode == 0, restore.stderr

    # The null-rationale row itself is untouched (not rewritten to "").
    async with get_session_factory()() as session:
        row = await session.get(TermTriageDecisionProjection, (await session.execute(
            text("select id from term_triage_decisions where run_id = :run_id"), {"run_id": run_id},
        )).scalar())
    assert row.rationale is None
