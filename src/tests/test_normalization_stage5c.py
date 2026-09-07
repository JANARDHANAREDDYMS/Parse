"""Focused, generated-fixture checks for Stage 5C contracts and boundaries."""

import uuid

import pytest

from maximor.jobs.types import JobType
from maximor.normalization.schemas import (
    FinalOrderFormExtraction,
    NormalizationResult,
    NormalizationRunStatus,
    ValidationStatus,
)
from maximor.normalization.versions import NORMALIZATION_RESULT_SCHEMA_VERSION, FINALIZATION_POLICY_VERSION


def _ids():
    return (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


def _result(status=NormalizationRunStatus.COMPLETED):
    org, doc, prep, analysis, term = _ids()
    extraction = FinalOrderFormExtraction(
        schema_version="0.1.0",
        organization_id=org,
        document_id=doc,
        preprocessing_run_id=prep,
        analysis_run_id=analysis,
        term_applicability_run_id=term,
        validation_status=ValidationStatus.COMPLETED if status is NormalizationRunStatus.COMPLETED else ValidationStatus.REVIEW_REQUIRED,
    )
    return NormalizationResult(
        schema_version=NORMALIZATION_RESULT_SCHEMA_VERSION,
        organization_id=org,
        document_id=doc,
        preprocessing_run_id=prep,
        analysis_run_id=analysis,
        term_applicability_run_id=term,
        finalization_policy_version=FINALIZATION_POLICY_VERSION,
        status=status,
        extraction=extraction,
    )


def test_normalization_result_preserves_terminal_domain_outcomes():
    for status in NormalizationRunStatus:
        result = _result(status)
        assert result.status is status
        assert result.analysis_run_id == result.extraction.analysis_run_id


def test_lineage_mismatch_is_rejected():
    result = _result()
    with pytest.raises(ValueError):
        payload = result.model_dump(mode="python")
        payload["document_id"] = uuid.uuid4()
        NormalizationResult(**payload)


def test_normalization_job_type_is_centralized():
    assert JobType.NORMALIZATION.value == "normalization"


def test_stage5c_migration_exists_and_is_chained():
    from pathlib import Path

    migration = Path(__file__).parents[1] / "maximor/db/migrations/versions/0016_normalization_stage5c.py"
    text = migration.read_text()
    assert 'revision = "0016_normalization_stage5c"' in text
    assert 'down_revision = "0015_term_enrichment_schema_fix"' in text
    for table in ("normalization_runs", "normalized_line_items", "normalization_issues", "normalization_semantic_findings"):
        assert f'"{table}"' in text
