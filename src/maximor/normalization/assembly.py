"""Assemble the trusted normalization input and per-candidate line-item source bundles.

Both functions here are pure and deterministic: given already-completed,
already-validated source artifacts, they perform identity/eligibility checks
and raise a typed `maximor.normalization.errors` exception the moment a
required source is missing, mismatched, or incomplete -- never silently
skipping an eligible candidate. Neither function performs database access,
evidence resolution, or a Claude call; `NormalizationRepository` is the only
caller responsible for loading the artifacts passed in here.
"""

import uuid

from maximor.document_analysis.schemas import ApplicabilityScope, DocumentAnalysisResult, EvidenceReference
from maximor.normalization.contracts import (
    LineItemSourceBundle,
    NormalizationInput,
    eligible_candidate_ids_of,
    expected_fact_fields,
    status_by_candidate_map,
)
from maximor.normalization.errors import (
    NormalizationCommercialFactCoverageMissingError,
    NormalizationSkuMappingMissingError,
    NormalizationSourceMismatchError,
)
from maximor.normalization.versions import LINE_ITEM_SOURCE_BUNDLE_SCHEMA_VERSION, NORMALIZATION_INPUT_SCHEMA_VERSION
from maximor.sku_mapping.contracts import SkuMappingRunArtifact
from maximor.sku_mapping.schemas import SkuMappingOutcome
from maximor.term_applicability.schemas import TermApplicabilityResult
from maximor.term_triage.schemas import TermTriageResult


def _merged_evidence(*groups: tuple[EvidenceReference, ...]) -> tuple[EvidenceReference, ...]:
    """Return a deterministic, deduplicated union of evidence across several groups."""

    seen: list[EvidenceReference] = []
    for group in groups:
        for reference in group:
            if reference not in seen:
                seen.append(reference)
    return tuple(seen)


def assemble_normalization_input(
    *,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    term_applicability_run_id: uuid.UUID,
    document_analysis: DocumentAnalysisResult,
    term_triage: TermTriageResult,
    term_applicability: TermApplicabilityResult,
    sku_mappings: dict[str, SkuMappingRunArtifact],
    sku_mapping_run_ids: dict[str, uuid.UUID],
    schema_version: str = NORMALIZATION_INPUT_SCHEMA_VERSION,
) -> NormalizationInput:
    """Validate identity alignment and eligible-candidate completeness, then construct.

    Raises `NormalizationSourceMismatchError` if any source disagrees about
    organization/document/analysis-run identity, `NormalizationSkuMappingMissingError`
    if an eligible candidate has no entry in `sku_mappings`/`sku_mapping_run_ids`,
    and `NormalizationCommercialFactCoverageMissingError` if an eligible
    candidate with expected raw-fact hints has no coverage declaration in
    `term_applicability.candidate_commercial_fact_coverage`. A candidate
    whose resolved `sku_mappings` entry exists but is not a `MATCH` is not an
    error here -- that is a complete, honest answer; only a *missing* entry
    is a construction failure.
    """

    if document_analysis.organization_id != organization_id or document_analysis.document_id != document_id:
        raise NormalizationSourceMismatchError
    for source in (term_triage, term_applicability):
        if (
            source.organization_id != organization_id
            or source.document_id != document_id
            or source.preprocessing_run_id != document_analysis.preprocessing_run_id
            or source.analysis_run_id != analysis_run_id
        ):
            raise NormalizationSourceMismatchError

    candidates_by_id = {candidate.candidate_id: candidate for candidate in document_analysis.product_candidates}
    status_by_candidate = status_by_candidate_map(document_analysis)
    coverage_by_candidate = {item.candidate_id: item for item in term_applicability.candidate_commercial_fact_coverage}

    for candidate_id in eligible_candidate_ids_of(document_analysis):
        if candidate_id not in sku_mappings or candidate_id not in sku_mapping_run_ids:
            raise NormalizationSkuMappingMissingError
        artifact = sku_mappings[candidate_id]
        if (
            artifact.task.organization_id != organization_id
            or artifact.task.document_id != document_id
            or artifact.task.analysis_run_id != analysis_run_id
            or artifact.decision.candidate_id != candidate_id
        ):
            raise NormalizationSourceMismatchError

        expected = expected_fact_fields(candidates_by_id[candidate_id], status_by_candidate[candidate_id])
        if expected and candidate_id not in coverage_by_candidate:
            raise NormalizationCommercialFactCoverageMissingError

    return NormalizationInput(
        schema_version=schema_version,
        organization_id=organization_id,
        document_id=document_id,
        preprocessing_run_id=document_analysis.preprocessing_run_id,
        analysis_run_id=analysis_run_id,
        term_applicability_run_id=term_applicability_run_id,
        document_analysis=document_analysis,
        term_triage=term_triage,
        term_applicability=term_applicability,
        sku_mappings=sku_mappings,
        sku_mapping_run_ids=sku_mapping_run_ids,
    )


def assemble_line_item_source_bundles(
    normalization_input: NormalizationInput,
    *,
    schema_version: str = LINE_ITEM_SOURCE_BUNDLE_SCHEMA_VERSION,
) -> tuple[LineItemSourceBundle, ...]:
    """Build one `LineItemSourceBundle` per eligible candidate with a MATCH SKU mapping.

    A candidate whose `sku_mappings` entry is `NO_MATCH` or `AMBIGUOUS` is
    skipped here, not an error -- `NormalizationInput` already guarantees
    every eligible candidate has *some* completed mapping; only a `MATCH`
    can ever become a line item. Document-wide terms are attached as
    `available_document_terms`, not inherited into the bundle's own facts.
    """

    candidates_by_id = {
        candidate.candidate_id: candidate
        for candidate in normalization_input.document_analysis.product_candidates
    }
    status_by_candidate = status_by_candidate_map(normalization_input.document_analysis)
    facts_by_candidate = {
        bundle.candidate_id: bundle
        for bundle in normalization_input.term_applicability.candidate_commercial_facts
    }
    coverage_by_candidate = {
        item.candidate_id: item
        for item in normalization_input.term_applicability.candidate_commercial_fact_coverage
    }
    document_terms = tuple(
        decision for decision in normalization_input.term_applicability.decisions
        if decision.applicability_scope is ApplicabilityScope.DOCUMENT
    )

    bundles: list[LineItemSourceBundle] = []
    for candidate_id in normalization_input.eligible_candidate_ids:
        artifact = normalization_input.sku_mappings[candidate_id]
        if artifact.decision.outcome is not SkuMappingOutcome.MATCH:
            continue

        candidate_terms = tuple(
            decision for decision in normalization_input.term_applicability.decisions
            if candidate_id in decision.applies_to_candidate_ids
        )
        facts = facts_by_candidate.get(candidate_id)
        coverage = coverage_by_candidate.get(candidate_id)
        product_candidate = candidates_by_id[candidate_id]

        evidence_groups: list[tuple[EvidenceReference, ...]] = [
            product_candidate.evidence,
            artifact.decision.evidence,
        ]
        if facts is not None:
            for fact in facts.facts:
                evidence_groups.append(fact.evidence)
        if coverage is not None:
            evidence_groups.append(coverage.evidence)
        for decision in candidate_terms:
            evidence_groups.append(decision.evidence)

        bundles.append(LineItemSourceBundle(
            schema_version=schema_version,
            candidate_id=candidate_id,
            commercial_status=status_by_candidate[candidate_id],
            product_candidate=product_candidate,
            sku_mapping_run_id=normalization_input.sku_mapping_run_ids[candidate_id],
            sku_mapping_decision=artifact.decision,
            candidate_commercial_facts=facts,
            candidate_commercial_fact_coverage=coverage,
            available_candidate_terms=candidate_terms,
            available_document_terms=document_terms,
            evidence=_merged_evidence(*evidence_groups),
        ))
    return tuple(bundles)
