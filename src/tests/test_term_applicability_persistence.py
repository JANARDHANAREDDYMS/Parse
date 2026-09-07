"""Database-backed tests for combined term-enrichment persistence.

All records are generated in the existing PostgreSQL test fixture; no supplied
documents or agent runtimes are used.
"""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from maximor.db.models import (
    CandidateCommercialFactProjection,
    CandidateCommercialFactCoverageProjection,
    TermApplicabilityDecisionProjection,
    TermApplicabilityEvidenceProjection,
    TermApplicabilityRun,
    TermTriageDecisionProjection,
)
from maximor.document_analysis.schemas import ApplicabilityScope, EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import BoundingBox, ExtractionSource
from maximor.storage import LocalObjectStorage
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService
from maximor.term_applicability.schemas import (
    CandidateCommercialFactCoverage,
    CandidateCommercialFacts,
    RawCommercialFact,
    RawCommercialFactField,
    TermApplicabilityDecision,
    TermApplicabilityResult,
    TermDisposition,
    compute_fact_id,
)
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION
from maximor.term_triage.schemas import TermTriageDecision, TermTriageDisposition, TermTriageResult
from maximor.term_triage.versions import TERM_TRIAGE_RESULT_SCHEMA_VERSION, TERM_TRIAGE_TASK_SCHEMA_VERSION
from maximor.db.session import get_session_factory
from maximor.document_analysis.agent import DocumentAnalysisRuntimeSummary
from test_sku_mapping_worker_integration import _seed


def _outputs(ids: dict) -> tuple[TermTriageResult, TermApplicabilityResult]:
    now = datetime.now(UTC)
    del now
    triage = TermTriageResult(
        schema_version=TERM_TRIAGE_TASK_SCHEMA_VERSION,
        organization_id=ids["organization_id"], document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(),
    )
    applicability = TermApplicabilityResult(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
        organization_id=ids["organization_id"], document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(), candidate_commercial_facts=(), candidate_commercial_fact_coverage=(),
    )
    return triage, applicability


@pytest.mark.asyncio
async def test_combined_result_persists_and_reloads_with_artifact_metadata(tmp_path, organization):
    """A completed combined artifact round-trips without reopening a source PDF."""
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    ids["organization_id"] = organization
    triage, applicability = _outputs(ids)
    service = TermApplicabilityPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1,
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, prompt_version="test",
        skill_version="test", agent_version="test", model="fake", started_at=datetime.now(UTC),
    )
    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=0)
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability, runtime=runtime)
    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)
    assert loaded.triage == triage and loaded.applicability == applicability
    async with get_session_factory()() as session:
        row = await session.get(TermApplicabilityRun, run_id)
        assert row is not None and row.status == "completed" and row.canonical_result_storage_key.endswith(".json.gz")
        assert not (await session.scalars(select(CandidateCommercialFactProjection).where(CandidateCommercialFactProjection.run_id == run_id))).all()
        assert not (await session.scalars(select(CandidateCommercialFactCoverageProjection).where(CandidateCommercialFactCoverageProjection.run_id == run_id))).all()


@pytest.mark.asyncio
async def test_failed_run_has_no_accepted_artifact_and_is_tenant_scoped(tmp_path, organization):
    """Failures expose only safe status metadata and cannot be loaded as results."""
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    run_id = uuid.uuid4()
    service = TermApplicabilityPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    await service.create_run(run_id=run_id, organization_id=organization, document_id=ids["document_id"], analysis_run_id=ids["analysis_run_id"], attempt_number=1, schema_version="v", prompt_version="p", skill_version="s", agent_version="a", model="fake", started_at=datetime.now(UTC))
    await service.mark_run_failed(run_id, error_code="validation_failed", error_message="safe failure")
    with pytest.raises(ValueError):
        await service.load_completed_result(organization_id=organization, run_id=run_id)
    with pytest.raises(ValueError):
        await service.load_completed_result(organization_id=uuid.uuid4(), run_id=run_id)


@pytest.mark.asyncio
async def test_triage_decision_with_none_rationale_persists_and_reloads_as_none(tmp_path, organization):
    """A triage decision with no rationale round-trips as SQL NULL, never an empty string.

    Regression for the migration 0013 nullable-inversion bug: before
    migration 0014, this raised a NOT NULL IntegrityError on
    `term_triage_decisions.rationale` even though the Pydantic contract
    always allowed `rationale=None`.
    """
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    ids["organization_id"] = organization
    triage = TermTriageResult(
        schema_version=TERM_TRIAGE_RESULT_SCHEMA_VERSION,
        organization_id=ids["organization_id"], document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(
            TermTriageDecision(term_id="term-no-rationale", disposition=TermTriageDisposition.UNCERTAIN, rationale=None),
            TermTriageDecision(term_id="term-with-rationale", disposition=TermTriageDisposition.POTENTIAL_LINE_ITEM, rationale="Pricing-adjacent term."),
        ),
    )
    _, applicability = _outputs(ids)
    service = TermApplicabilityPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1,
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, prompt_version="test",
        skill_version="test", agent_version="test", model="fake", started_at=datetime.now(UTC),
    )
    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=0)

    # Persists without raising -- this alone is the regression check.
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability, runtime=runtime)

    # Canonical artifact reload preserves None exactly (never "" or a placeholder).
    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)
    reloaded_by_term = {d.term_id: d for d in loaded.triage.decisions}
    assert reloaded_by_term["term-no-rationale"].rationale is None
    assert reloaded_by_term["term-with-rationale"].rationale == "Pricing-adjacent term."

    # The relational projection stores SQL NULL, not an empty string, and
    # leaves the non-null row's rationale untouched.
    async with get_session_factory()() as session:
        rows = (await session.scalars(
            select(TermTriageDecisionProjection).where(TermTriageDecisionProjection.run_id == run_id)
        )).all()
    projection_by_term = {row.term_id: row.rationale for row in rows}
    assert projection_by_term["term-no-rationale"] is None
    assert projection_by_term["term-with-rationale"] == "Pricing-adjacent term."


@pytest.mark.asyncio
async def test_non_empty_evidence_rows_persist_and_reload_with_correct_lineage(tmp_path, organization):
    """Regression for the missing `TermApplicabilityEvidenceProjection` import.

    Every prior persistence test used empty decisions/facts/coverage, so
    `save_completed_result`'s evidence_rows loop never actually iterated and
    the missing import was never exercised -- confirmed live in a real
    `of-0006` pipeline run, whose first fully accepted result (with real
    evidence) failed with a `NameError` at this exact call site. This test
    submits a term decision, a candidate fact, and a candidate fact-coverage
    declaration that each carry non-empty evidence, so the loop runs for
    all three `owner_type` branches (`term_decision`, `commercial_fact`,
    `fact_coverage`).
    """
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    ids["organization_id"] = organization
    evidence = EvidenceReference(
        preprocessing_run_id=ids["preprocessing_run_id"], page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
        bounding_box=BoundingBox(x0=1, y0=2, x1=30, y1=12),
    )
    triage = TermTriageResult(
        schema_version=TERM_TRIAGE_RESULT_SCHEMA_VERSION,
        organization_id=ids["organization_id"], document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(TermTriageDecision(term_id="term-priced", disposition=TermTriageDisposition.POTENTIAL_LINE_ITEM, rationale="Pricing term."),),
    )
    fact = RawCommercialFact(
        fact_id=compute_fact_id(candidate_id="candidate-purchased", field=RawCommercialFactField.QUANTITY, raw_value="10"),
        candidate_id="candidate-purchased", field=RawCommercialFactField.QUANTITY, raw_value="10", evidence=(evidence,),
    )
    applicability = TermApplicabilityResult(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
        organization_id=ids["organization_id"], document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(
            TermApplicabilityDecision(
                schema_version="1.0.0", term_id="term-priced", disposition=TermDisposition.LINE_ITEM,
                applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence,),
            ),
        ),
        candidate_commercial_facts=(CandidateCommercialFacts(candidate_id="candidate-purchased", facts=(fact,)),),
        candidate_commercial_fact_coverage=(
            CandidateCommercialFactCoverage(
                candidate_id="candidate-purchased",
                expected_fields=(RawCommercialFactField.QUANTITY,), extracted_fields=(RawCommercialFactField.QUANTITY,),
                unresolved_fields=(), evidence=(evidence,),
            ),
        ),
    )
    service = TermApplicabilityPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1,
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, prompt_version="test",
        skill_version="test", agent_version="test", model="fake", started_at=datetime.now(UTC),
    )
    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=0)

    # This is the regression check: previously raised NameError.
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability, runtime=runtime)

    async with get_session_factory()() as session:
        evidence_rows = (await session.scalars(
            select(TermApplicabilityEvidenceProjection).where(TermApplicabilityEvidenceProjection.run_id == run_id)
        )).all()
    assert len(evidence_rows) == 3  # one per owner: term_decision, commercial_fact, fact_coverage
    owner_types = {row.owner_type for row in evidence_rows}
    assert owner_types == {"term_decision", "commercial_fact", "fact_coverage"}
    for row in evidence_rows:
        assert row.run_id == run_id
        assert row.organization_id == organization
        assert row.preprocessing_run_id == ids["preprocessing_run_id"]
        assert row.external_block_id == "native:p0001:b000000"
        assert row.representation == EvidenceRepresentation.NATIVE_TEXT.value

    async with get_session_factory()() as session:
        decision_row = (await session.scalars(
            select(TermApplicabilityDecisionProjection).where(TermApplicabilityDecisionProjection.run_id == run_id)
        )).one()
    assert decision_row.evidence == [evidence.model_dump(mode="json")]

    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)
    assert loaded.applicability.decisions[0].evidence == (evidence,)
    assert loaded.applicability.candidate_commercial_facts[0].facts[0].evidence == (evidence,)
    assert loaded.applicability.candidate_commercial_fact_coverage[0].evidence == (evidence,)


@pytest.mark.asyncio
async def test_coverage_projection_and_reload_store_the_application_computed_extracted_fields(tmp_path, organization):
    """`CandidateCommercialFactCoverageProjection.extracted_fields` reflects the application-computed value.

    Mirrors what `ClaudeTermApplicabilityAgent._resolve_coverage_evidence_ids`
    now always computes server-side from a submission's own facts (never a
    value Claude restates) -- this test confirms persistence faithfully
    stores and reloads whatever the application computed, for two fields
    where one was extracted and one was left unresolved.
    """
    ids = await _seed(organization, LocalObjectStorage(tmp_path))
    ids["organization_id"] = organization
    evidence = EvidenceReference(
        preprocessing_run_id=ids["preprocessing_run_id"], page_number=1, block_id="native:p0001:b000000",
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
        bounding_box=BoundingBox(x0=1, y0=2, x1=30, y1=12),
    )
    triage = TermTriageResult(
        schema_version=TERM_TRIAGE_RESULT_SCHEMA_VERSION,
        organization_id=ids["organization_id"], document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(),
    )
    quantity_fact = RawCommercialFact(
        fact_id=compute_fact_id(candidate_id="candidate-purchased", field=RawCommercialFactField.QUANTITY, raw_value="10"),
        candidate_id="candidate-purchased", field=RawCommercialFactField.QUANTITY, raw_value="10", evidence=(evidence,),
    )
    # The application computes `extracted_fields` as the sorted set of
    # fields actually present in the submitted facts -- exactly this
    # derivation, reproduced here rather than hand-picked, to prove
    # persistence stores that computed value faithfully.
    extracted_fields = tuple(sorted({quantity_fact.field}, key=lambda item: item.value))
    coverage = CandidateCommercialFactCoverage(
        candidate_id="candidate-purchased",
        expected_fields=(RawCommercialFactField.QUANTITY, RawCommercialFactField.UNIT_PRICE),
        extracted_fields=extracted_fields, unresolved_fields=(RawCommercialFactField.UNIT_PRICE,),
        evidence=(evidence,),
    )
    applicability = TermApplicabilityResult(
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION,
        organization_id=ids["organization_id"], document_id=ids["document_id"],
        preprocessing_run_id=ids["preprocessing_run_id"], analysis_run_id=ids["analysis_run_id"],
        decisions=(), candidate_commercial_facts=(CandidateCommercialFacts(candidate_id="candidate-purchased", facts=(quantity_fact,)),),
        candidate_commercial_fact_coverage=(coverage,),
    )
    service = TermApplicabilityPersistenceService(get_session_factory(), LocalObjectStorage(tmp_path))
    run_id = uuid.uuid4()
    await service.create_run(
        run_id=run_id, organization_id=organization, document_id=ids["document_id"],
        analysis_run_id=ids["analysis_run_id"], attempt_number=1,
        schema_version=TERM_APPLICABILITY_RESULT_SCHEMA_VERSION, prompt_version="test",
        skill_version="test", agent_version="test", model="fake", started_at=datetime.now(UTC),
    )
    runtime = DocumentAnalysisRuntimeSummary(request_id=uuid.uuid4(), started_at=datetime.now(UTC), tool_call_count=0)
    await service.save_completed_result(run_id=run_id, triage=triage, applicability=applicability, runtime=runtime)

    async with get_session_factory()() as session:
        projection = (await session.scalars(
            select(CandidateCommercialFactCoverageProjection).where(CandidateCommercialFactCoverageProjection.run_id == run_id)
        )).one()
    assert projection.extracted_fields == ["quantity"]
    assert projection.unresolved_fields == ["unit_price"]

    loaded = await service.load_completed_result(organization_id=organization, run_id=run_id)
    reloaded_coverage = loaded.applicability.candidate_commercial_fact_coverage[0]
    assert reloaded_coverage.extracted_fields == (RawCommercialFactField.QUANTITY,)
    assert reloaded_coverage.unresolved_fields == (RawCommercialFactField.UNIT_PRICE,)
