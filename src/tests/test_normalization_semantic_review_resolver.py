"""Pure unit tests for `NormalizationEvidenceResolver`.

No database, no PDF, no paid Claude call -- everything is generated
in-memory, reusing the same fixture builders `test_normalization_assembly.py`
already exposes. This file exercises only `index_evidence_by_id`/
`NormalizationEvidenceResolver.resolve`, not the handler wiring (covered by
`test_normalization_stage5c_worker.py`).
"""

import pytest

from maximor.normalization.assembly import assemble_normalization_input
from maximor.normalization.schemas import FinalOrderFormExtraction
from maximor.normalization.semantic_review.errors import SemanticReviewTaskError
from maximor.normalization.semantic_review.resolver import NormalizationEvidenceResolver, index_evidence_by_id
from maximor.normalization.service import assemble_normalized_draft
from test_normalization_assembly import (
    ANALYSIS_RUN_ID,
    DOCUMENT_ID,
    ORGANIZATION_ID,
    PREPROCESSING_RUN_ID,
    TERM_APPLICABILITY_RUN_ID,
    base_scenario,
    evidence,
)


class _FakeEvidenceResolver:
    """Record the exact request it received and return a fixed sentinel."""

    def __init__(self):
        self.requests: list = []

    async def get_evidence_region(self, request):
        self.requests.append(request)
        return {"block_id": request.evidence.block_id or request.evidence.table_id}


def _normalization_input():
    scenario = base_scenario()
    return assemble_normalization_input(
        organization_id=ORGANIZATION_ID, document_id=DOCUMENT_ID, analysis_run_id=ANALYSIS_RUN_ID,
        term_applicability_run_id=TERM_APPLICABILITY_RUN_ID,
        document_analysis=scenario["document_analysis"], term_triage=scenario["term_triage"],
        term_applicability=scenario["term_applicability"], sku_mappings=scenario["sku_mappings"],
        sku_mapping_run_ids=scenario["sku_mapping_run_ids"],
    )


def test_index_includes_candidate_sku_mapping_and_fact_evidence():
    normalization_input = _normalization_input()
    extraction = assemble_normalized_draft(normalization_input)
    index = index_evidence_by_id(normalization_input, extraction)

    # candidate-hinted's own evidence, its MATCH decision's evidence, and its
    # extracted quantity fact's evidence must all be indexed.
    assert evidence().block_id in index
    assert evidence("native:p0001:b000003").block_id in index  # the quantity fact's own evidence
    assert evidence("native:p0001:b000004").block_id in index  # the coverage declaration's evidence


def test_index_includes_finalized_line_item_field_provenance_evidence():
    normalization_input = _normalization_input()
    extraction = assemble_normalized_draft(normalization_input)
    index = index_evidence_by_id(normalization_input, extraction)

    hinted = next(item for item in extraction.line_items if item.source_candidate_id == "candidate-hinted")
    for provenance in hinted.field_provenance.values():
        for reference in provenance.evidence:
            identifier = reference.block_id or reference.table_id
            assert identifier in index


@pytest.mark.asyncio
async def test_resolve_delegates_to_the_underlying_resolver_with_correct_scope():
    normalization_input = _normalization_input()
    extraction = assemble_normalized_draft(normalization_input)
    fake = _FakeEvidenceResolver()
    resolver = NormalizationEvidenceResolver(normalization_input, extraction, fake)

    result = await resolver.resolve(evidence().block_id)

    assert result == {"block_id": evidence().block_id}
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.organization_id == ORGANIZATION_ID
    assert request.preprocessing_run_id == PREPROCESSING_RUN_ID
    assert request.evidence.block_id == evidence().block_id


@pytest.mark.asyncio
async def test_resolve_rejects_an_id_absent_from_the_trusted_index():
    normalization_input = _normalization_input()
    extraction = assemble_normalized_draft(normalization_input)
    resolver = NormalizationEvidenceResolver(normalization_input, extraction, _FakeEvidenceResolver())

    with pytest.raises(SemanticReviewTaskError) as excinfo:
        await resolver.resolve("native:p0001:b999999")
    assert excinfo.value.code == "evidence_not_indexed"


def test_index_omits_ids_never_cited_by_any_trusted_source():
    normalization_input = _normalization_input()
    extraction = assemble_normalized_draft(normalization_input)
    index = index_evidence_by_id(normalization_input, extraction)
    assert "native:p0001:b999999" not in index
