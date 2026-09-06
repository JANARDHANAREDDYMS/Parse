"""Test the SKU-mapping decision output contract without database, catalog, or agent work."""

import uuid

import pytest
from pydantic import ValidationError

from maximor.document_analysis.schemas import EvidenceReference, EvidenceRepresentation
from maximor.preprocessing.schemas import ExtractionSource
from maximor.sku_mapping.schemas import SkuMappingDecision, SkuMappingOutcome
from maximor.sku_mapping.versions import SKU_MAPPING_DECISION_SCHEMA_VERSION


def _evidence(preprocessing_run_id: uuid.UUID, block_id: str = "native:p0001:b000000") -> EvidenceReference:
    return EvidenceReference(
        preprocessing_run_id=preprocessing_run_id,
        page_number=1,
        block_id=block_id,
        representation=EvidenceRepresentation.NATIVE_TEXT,
        extraction_source=ExtractionSource.NATIVE,
    )


def _base_kwargs(preprocessing_run_id: uuid.UUID) -> dict:
    return dict(
        schema_version=SKU_MAPPING_DECISION_SCHEMA_VERSION,
        organization_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        preprocessing_run_id=preprocessing_run_id,
        analysis_run_id=uuid.uuid4(),
        candidate_id="candidate-0001",
        catalog_version_id=uuid.uuid4(),
        evidence=(_evidence(preprocessing_run_id),),
    )


def test_match_decision_requires_sku_id_code_and_name_together():
    """Accept a valid MATCH and reject one missing any of sku_id/sku_code/sku_name."""

    preprocessing_run_id = uuid.uuid4()
    decision = SkuMappingDecision(
        **_base_kwargs(preprocessing_run_id),
        outcome=SkuMappingOutcome.MATCH,
        sku_id=uuid.uuid4(), sku_code="TALENT_ACQUISITION", sku_name="Talent Acquisition",
    )
    assert decision.outcome is SkuMappingOutcome.MATCH

    with pytest.raises(ValidationError):
        SkuMappingDecision(**_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.MATCH, sku_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        SkuMappingDecision(
            **_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.MATCH,
            sku_id=uuid.uuid4(), sku_code="TALENT_ACQUISITION",
        )


def test_non_match_outcomes_must_not_carry_sku_fields():
    """Reject a NO_MATCH or AMBIGUOUS decision that still names a chosen SKU."""

    preprocessing_run_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        SkuMappingDecision(
            **_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.NO_MATCH,
            sku_id=uuid.uuid4(), sku_code="X", sku_name="X",
        )
    with pytest.raises(ValidationError):
        SkuMappingDecision(
            **_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.AMBIGUOUS,
            considered_sku_ids=(uuid.uuid4(), uuid.uuid4()), sku_id=uuid.uuid4(),
        )


def test_no_match_decision_is_valid_without_any_sku():
    """Accept a plain NO_MATCH decision naming nothing."""

    preprocessing_run_id = uuid.uuid4()
    decision = SkuMappingDecision(**_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.NO_MATCH)
    assert decision.sku_id is None
    assert decision.considered_sku_ids == ()


def test_ambiguous_decision_requires_at_least_two_distinct_considered_skus():
    """Reject AMBIGUOUS with zero, one, or duplicated considered SKUs."""

    preprocessing_run_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        SkuMappingDecision(**_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.AMBIGUOUS)
    one = uuid.uuid4()
    with pytest.raises(ValidationError):
        SkuMappingDecision(**_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.AMBIGUOUS, considered_sku_ids=(one,))
    with pytest.raises(ValidationError):
        SkuMappingDecision(**_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.AMBIGUOUS, considered_sku_ids=(one, one))


def test_ambiguous_decision_is_valid_with_two_distinct_considered_skus():
    """Accept a genuine AMBIGUOUS decision naming what evidence could not distinguish."""

    preprocessing_run_id = uuid.uuid4()
    ids = (uuid.uuid4(), uuid.uuid4())
    decision = SkuMappingDecision(
        **_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.AMBIGUOUS, considered_sku_ids=ids,
    )
    assert set(decision.considered_sku_ids) == set(ids)


def test_match_decision_must_not_carry_considered_sku_ids():
    """Reject a MATCH decision that also lists ambiguity candidates."""

    preprocessing_run_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        SkuMappingDecision(
            **_base_kwargs(preprocessing_run_id), outcome=SkuMappingOutcome.MATCH,
            sku_id=uuid.uuid4(), sku_code="X", sku_name="X",
            considered_sku_ids=(uuid.uuid4(), uuid.uuid4()),
        )


def test_every_outcome_requires_at_least_one_evidence_reference():
    """Reject NO_MATCH/AMBIGUOUS/MATCH decisions carrying no evidence at all."""

    preprocessing_run_id = uuid.uuid4()
    kwargs = _base_kwargs(preprocessing_run_id)
    kwargs["evidence"] = ()
    with pytest.raises(ValidationError):
        SkuMappingDecision(**kwargs, outcome=SkuMappingOutcome.NO_MATCH)


def test_decision_rejects_evidence_from_another_preprocessing_run():
    """Reject evidence that does not belong to the decision's own preprocessing run."""

    preprocessing_run_id = uuid.uuid4()
    kwargs = _base_kwargs(preprocessing_run_id)
    kwargs["evidence"] = (_evidence(uuid.uuid4()),)
    with pytest.raises(ValidationError):
        SkuMappingDecision(**kwargs, outcome=SkuMappingOutcome.NO_MATCH)
