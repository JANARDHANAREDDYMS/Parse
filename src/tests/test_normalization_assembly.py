"""Test pure, deterministic normalization trusted-input assembly.

No database, no PDF, no paid Claude call anywhere in this file -- every
source (`DocumentAnalysisResult`, `TermTriageResult`, `TermApplicabilityResult`,
`SkuMappingRunArtifact`) is a generated in-memory fixture. Builder functions
here are reused by `test_normalization_contracts.py`, mirroring how
`test_sku_mapping_worker_integration._seed` is reused across this codebase's
other test files.
"""

import uuid

import pytest

from maximor.document_analysis.schemas import (
    ApplicabilityScope,
    CommercialStatus,
    CommercialStatusAssessment,
    DocumentAnalysisResult,
    EvidenceReference,
    EvidenceRepresentation,
    ExtractionSource,
    ProductCandidate,
)
from maximor.normalization.assembly import assemble_line_item_source_bundles, assemble_normalization_input
from maximor.normalization.errors import (
    NormalizationCommercialFactCoverageMissingError,
    NormalizationSkuMappingMissingError,
    NormalizationSourceMismatchError,
)
from maximor.sku_mapping.contracts import SkuMappingRunArtifact, SkuMappingTask
from maximor.sku_mapping.schemas import (
    RetrievedSku,
    SkuMappingDecision,
    SkuMappingOutcome,
    SkuMatchSource,
    SkuRecord,
    SkuRetrievalResult,
)
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
from maximor.term_triage.schemas import TermTriageResult

ORGANIZATION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
PREPROCESSING_RUN_ID = uuid.uuid4()
ANALYSIS_RUN_ID = uuid.uuid4()
TERM_APPLICABILITY_RUN_ID = uuid.uuid4()


def evidence(block_id: str = "native:p0001:b000000") -> EvidenceReference:
    """Build one evidence reference against the shared fixture preprocessing run."""

    return EvidenceReference(
        preprocessing_run_id=PREPROCESSING_RUN_ID, page_number=1, block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT, extraction_source=ExtractionSource.NATIVE,
    )


def document_analysis(*, candidates, statuses) -> DocumentAnalysisResult:
    """Build one document-analysis result sharing this module's fixed identity."""

    return DocumentAnalysisResult(
        schema_version="analysis-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, preprocessing_schema_version="prep-v1",
        prompt_version="p1", agent_version="a1", product_candidates=candidates, commercial_statuses=statuses,
    )


def term_triage_result() -> TermTriageResult:
    """Build one empty triage result sharing this module's fixed identity."""

    return TermTriageResult(
        schema_version="triage-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID, decisions=(),
    )


def term_applicability_result(*, decisions=(), facts=(), coverage=()) -> TermApplicabilityResult:
    """Build one term-applicability result sharing this module's fixed identity."""

    return TermApplicabilityResult(
        schema_version="applicability-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID,
        decisions=decisions, candidate_commercial_facts=facts, candidate_commercial_fact_coverage=coverage,
    )


def match_artifact(candidate_id: str, *, raw_attributes: dict | None = None, sku_code: str = "PREMIUM") -> SkuMappingRunArtifact:
    """Build one MATCH SKU-mapping artifact for one candidate."""

    catalog_version_id = uuid.uuid4()
    sku = SkuRecord(
        id=uuid.uuid4(), source_sku_id=uuid.uuid4(), organization_id=ORGANIZATION_ID,
        catalog_version_id=catalog_version_id, sku_code=sku_code, name=sku_code.title(),
    )
    task = SkuMappingTask(
        schema_version="mapping-task-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID,
        document_analysis_schema_version="analysis-v1", document_analysis_agent_version="a1",
        candidate_id=candidate_id, raw_name="Thing", raw_attributes=raw_attributes or {},
        commercial_status=CommercialStatus.PURCHASED, candidate_evidence=(evidence(),),
    )
    retrieval = SkuRetrievalResult(
        schema_version="retrieval-v1", retriever_version="retriever-v1", organization_id=ORGANIZATION_ID,
        catalog_version_id=catalog_version_id, catalog_version_identifier="v1", candidate_id=candidate_id,
        candidates=(RetrievedSku(sku=sku, score=1.0, matched_sources=(SkuMatchSource.EXACT,)),),
    )
    decision = SkuMappingDecision(
        schema_version="decision-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID, candidate_id=candidate_id,
        catalog_version_id=catalog_version_id, outcome=SkuMappingOutcome.MATCH,
        sku_id=sku.id, sku_code=sku.sku_code, sku_name=sku.name, evidence=(evidence(),),
    )
    return SkuMappingRunArtifact(schema_version="artifact-v1", task=task, retrieval=retrieval, decision=decision)


def no_match_artifact(candidate_id: str) -> SkuMappingRunArtifact:
    """Build one NO_MATCH SKU-mapping artifact for one candidate."""

    catalog_version_id = uuid.uuid4()
    task = SkuMappingTask(
        schema_version="mapping-task-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID,
        document_analysis_schema_version="analysis-v1", document_analysis_agent_version="a1",
        candidate_id=candidate_id, raw_name="Thing", commercial_status=CommercialStatus.PURCHASED,
        candidate_evidence=(evidence(),),
    )
    retrieval = SkuRetrievalResult(
        schema_version="retrieval-v1", retriever_version="retriever-v1", organization_id=ORGANIZATION_ID,
        catalog_version_id=catalog_version_id, catalog_version_identifier="v1", candidate_id=candidate_id,
    )
    decision = SkuMappingDecision(
        schema_version="decision-v1", organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID,
        preprocessing_run_id=PREPROCESSING_RUN_ID, analysis_run_id=ANALYSIS_RUN_ID, candidate_id=candidate_id,
        catalog_version_id=catalog_version_id, outcome=SkuMappingOutcome.NO_MATCH, evidence=(evidence(),),
    )
    return SkuMappingRunArtifact(schema_version="artifact-v1", task=task, retrieval=retrieval, decision=decision)


def base_scenario():
    """Build a small, complete, aligned normalization scenario.

    `candidate-plain` is purchased with no raw-attribute hints (no coverage
    required). `candidate-hinted` is purchased with a quantity hint (coverage
    required) and has a real extracted fact plus a matching coverage
    declaration. `candidate-excluded` is excluded and must never require or
    receive a SKU mapping.
    """

    candidates = (
        ProductCandidate(candidate_id="candidate-excluded", raw_name="Excluded Thing", evidence=(evidence("native:p0001:b000002"),)),
        ProductCandidate(candidate_id="candidate-hinted", raw_name="Hinted Thing", raw_attributes={"qty": "2"}, evidence=(evidence("native:p0001:b000001"),)),
        ProductCandidate(candidate_id="candidate-plain", raw_name="Plain Thing", evidence=(evidence(),)),
    )
    statuses = (
        CommercialStatusAssessment(assessment_id="status-excluded", status=CommercialStatus.EXCLUDED, candidate_id="candidate-excluded", evidence=(evidence("native:p0001:b000002"),)),
        CommercialStatusAssessment(assessment_id="status-hinted", status=CommercialStatus.PURCHASED, candidate_id="candidate-hinted", evidence=(evidence("native:p0001:b000001"),)),
        CommercialStatusAssessment(assessment_id="status-plain", status=CommercialStatus.PURCHASED, candidate_id="candidate-plain", evidence=(evidence(),)),
    )
    analysis = document_analysis(candidates=candidates, statuses=statuses)

    document_term = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-document", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.DOCUMENT, evidence=(evidence(),),
    )
    candidate_term = TermApplicabilityDecision(
        schema_version="1.0.0", term_id="term-candidate", disposition=TermDisposition.LINE_ITEM,
        applicability_scope=ApplicabilityScope.CANDIDATE, applies_to_candidate_ids=("candidate-plain",),
        evidence=(evidence(),),
    )
    fact = RawCommercialFact(
        fact_id=compute_fact_id(candidate_id="candidate-hinted", field=RawCommercialFactField.QUANTITY, raw_value="2"),
        candidate_id="candidate-hinted", field=RawCommercialFactField.QUANTITY, raw_value="2", evidence=(evidence("native:p0001:b000003"),),
    )
    facts = (CandidateCommercialFacts(candidate_id="candidate-hinted", facts=(fact,)),)
    coverage = (CandidateCommercialFactCoverage(
        candidate_id="candidate-hinted", expected_fields=(RawCommercialFactField.QUANTITY,),
        extracted_fields=(RawCommercialFactField.QUANTITY,), unresolved_fields=(),
        evidence=(evidence("native:p0001:b000004"),),
    ),)
    applicability = term_applicability_result(decisions=(candidate_term, document_term), facts=facts, coverage=coverage)

    sku_mappings = {
        "candidate-plain": match_artifact("candidate-plain"),
        "candidate-hinted": match_artifact("candidate-hinted", raw_attributes={"qty": "2"}, sku_code="HINTED"),
    }
    sku_mapping_run_ids = {"candidate-plain": uuid.uuid4(), "candidate-hinted": uuid.uuid4()}

    return dict(
        document_analysis=analysis, term_triage=term_triage_result(), term_applicability=applicability,
        sku_mappings=sku_mappings, sku_mapping_run_ids=sku_mapping_run_ids,
        document_term=document_term, candidate_term=candidate_term, fact=fact, coverage=coverage[0],
    )


def _assemble(**overrides):
    scenario = base_scenario()
    scenario.update(overrides)
    return assemble_normalization_input(
        organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
        term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
        document_analysis=scenario["document_analysis"], term_triage=scenario["term_triage"],
        term_applicability=scenario["term_applicability"], sku_mappings=scenario["sku_mappings"],
        sku_mapping_run_ids=scenario["sku_mapping_run_ids"],
    )


def test_completed_aligned_sources_assemble_successfully():
    normalization_input = _assemble()
    assert normalization_input.eligible_candidate_ids == ("candidate-hinted", "candidate-plain")
    assert set(normalization_input.sku_mappings) == {"candidate-plain", "candidate-hinted"}


def test_tenant_document_analysis_run_mismatch_is_rejected():
    scenario = base_scenario()
    mismatched_applicability = scenario["term_applicability"].model_copy(update={"document_id": uuid.uuid4()})
    with pytest.raises(NormalizationSourceMismatchError):
        assemble_normalization_input(
            organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
            term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
            document_analysis=scenario["document_analysis"], term_triage=scenario["term_triage"],
            term_applicability=mismatched_applicability, sku_mappings=scenario["sku_mappings"],
            sku_mapping_run_ids=scenario["sku_mapping_run_ids"],
        )


def test_excluded_candidate_creates_no_line_item_bundle():
    normalization_input = _assemble()
    bundles = assemble_line_item_source_bundles(normalization_input)
    assert "candidate-excluded" not in {bundle.candidate_id for bundle in bundles}
    assert "candidate-excluded" not in normalization_input.sku_mappings


def test_optional_and_mentioned_and_ambiguous_candidates_create_no_bundle():
    candidates = (
        ProductCandidate(candidate_id="candidate-ambiguous", raw_name="Ambiguous", evidence=(evidence("native:p0001:b000002"),)),
        ProductCandidate(candidate_id="candidate-mentioned", raw_name="Mentioned", evidence=(evidence("native:p0001:b000001"),)),
        ProductCandidate(candidate_id="candidate-optional", raw_name="Optional", evidence=(evidence(),)),
    )
    statuses = (
        CommercialStatusAssessment(assessment_id="s1", status=CommercialStatus.AMBIGUOUS, candidate_id="candidate-ambiguous", evidence=(evidence("native:p0001:b000002"),)),
        CommercialStatusAssessment(assessment_id="s2", status=CommercialStatus.MENTIONED, candidate_id="candidate-mentioned", evidence=(evidence("native:p0001:b000001"),)),
        CommercialStatusAssessment(assessment_id="s3", status=CommercialStatus.OPTIONAL, candidate_id="candidate-optional", evidence=(evidence(),)),
    )
    analysis = document_analysis(candidates=candidates, statuses=statuses)
    applicability = term_applicability_result()
    normalization_input = assemble_normalization_input(
        organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
        term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
        document_analysis=analysis, term_triage=term_triage_result(), term_applicability=applicability,
        sku_mappings={}, sku_mapping_run_ids={},
    )
    assert normalization_input.eligible_candidate_ids == ()
    assert assemble_line_item_source_bundles(normalization_input) == ()


def test_purchased_candidate_without_completed_sku_mapping_is_surfaced_not_skipped():
    scenario = base_scenario()
    incomplete_mappings = dict(scenario["sku_mappings"])
    incomplete_run_ids = dict(scenario["sku_mapping_run_ids"])
    del incomplete_mappings["candidate-hinted"]
    del incomplete_run_ids["candidate-hinted"]
    with pytest.raises(NormalizationSkuMappingMissingError):
        assemble_normalization_input(
            organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
            term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
            document_analysis=scenario["document_analysis"], term_triage=scenario["term_triage"],
            term_applicability=scenario["term_applicability"], sku_mappings=incomplete_mappings,
            sku_mapping_run_ids=incomplete_run_ids,
        )


def test_purchased_candidate_without_complete_fact_coverage_is_surfaced():
    scenario = base_scenario()
    applicability_without_coverage = term_applicability_result(
        decisions=(scenario["candidate_term"], scenario["document_term"]),
        facts=scenario["term_applicability"].candidate_commercial_facts,
        coverage=(),
    )
    with pytest.raises(NormalizationCommercialFactCoverageMissingError):
        assemble_normalization_input(
            organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
            term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
            document_analysis=scenario["document_analysis"], term_triage=scenario["term_triage"],
            term_applicability=applicability_without_coverage, sku_mappings=scenario["sku_mappings"],
            sku_mapping_run_ids=scenario["sku_mapping_run_ids"],
        )


def test_no_match_candidate_is_not_an_assembly_error_but_yields_no_bundle():
    scenario = base_scenario()
    mappings = dict(scenario["sku_mappings"])
    mappings["candidate-plain"] = no_match_artifact("candidate-plain")
    normalization_input = assemble_normalization_input(
        organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
        term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
        document_analysis=scenario["document_analysis"], term_triage=scenario["term_triage"],
        term_applicability=scenario["term_applicability"], sku_mappings=mappings,
        sku_mapping_run_ids=scenario["sku_mapping_run_ids"],
    )
    bundle_ids = {bundle.candidate_id for bundle in assemble_line_item_source_bundles(normalization_input)}
    assert bundle_ids == {"candidate-hinted"}


def test_source_bundle_preserves_sku_candidate_term_fact_and_evidence_lineage():
    normalization_input = _assemble()
    bundles = {bundle.candidate_id: bundle for bundle in assemble_line_item_source_bundles(normalization_input)}

    plain = bundles["candidate-plain"]
    assert plain.product_candidate.candidate_id == "candidate-plain"
    assert plain.sku_mapping_decision.sku_code == "PREMIUM"
    assert plain.candidate_commercial_facts is None
    assert [d.term_id for d in plain.available_candidate_terms] == ["term-candidate"]
    assert [d.term_id for d in plain.available_document_terms] == ["term-document"]
    assert evidence() in plain.evidence

    hinted = bundles["candidate-hinted"]
    assert hinted.sku_mapping_decision.sku_code == "HINTED"
    assert hinted.candidate_commercial_facts.facts[0].fact_id == compute_fact_id(
        candidate_id="candidate-hinted", field=RawCommercialFactField.QUANTITY, raw_value="2",
    )
    assert hinted.candidate_commercial_fact_coverage.extracted_fields == (RawCommercialFactField.QUANTITY,)
    assert hinted.available_candidate_terms == ()
    assert evidence("native:p0001:b000003") in hinted.evidence
    assert evidence("native:p0001:b000004") in hinted.evidence
    assert hinted.sku_mapping_run_id == normalization_input.sku_mapping_run_ids["candidate-hinted"]


def test_output_contains_no_pdf_path_storage_key_or_sql_session_reference():
    normalization_input = _assemble()
    bundles = assemble_line_item_source_bundles(normalization_input)
    dumped = normalization_input.model_dump(mode="json")
    bundle_dumps = [bundle.model_dump(mode="json") for bundle in bundles]

    def _flatten_text(value) -> str:
        import json
        return json.dumps(value)

    forbidden_substrings = (".pdf", "storage_key", "/tmp/", "Session", "session_factory", "sqlalchemy")
    for payload in (dumped, *bundle_dumps):
        text = _flatten_text(payload)
        for forbidden in forbidden_substrings:
            assert forbidden not in text
