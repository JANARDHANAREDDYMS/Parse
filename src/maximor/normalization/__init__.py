"""Expose normalization contracts, trusted-input assembly, and the read-only repository.

Imports perform no database access by themselves. A caller explicitly
constructs and invokes `NormalizationRepository`. Nothing in this package
runs an agent, parses money/dates/quantities, does arithmetic, or reopens a
PDF -- Stage 1 defines final-output contracts and assembles already-completed
upstream results into a trusted input; deterministic normalization itself is
a later stage.
"""

from maximor.normalization.assembly import assemble_line_item_source_bundles, assemble_normalization_input
from maximor.normalization.contracts import (
    LineItemSourceBundle,
    NormalizationInput,
    NormalizationRequest,
    eligible_candidate_ids_of,
    expected_fact_fields,
    status_by_candidate_map,
)
from maximor.normalization.errors import (
    NormalizationAnalysisRunNotCompletedError,
    NormalizationAnalysisRunNotFoundError,
    NormalizationBundleConstructionError,
    NormalizationCommercialFactCoverageMissingError,
    NormalizationDocumentNotFoundError,
    NormalizationError,
    NormalizationInputConstructionError,
    NormalizationOrganizationNotFoundError,
    NormalizationSkuMappingMissingError,
    NormalizationSourceError,
    NormalizationSourceMismatchError,
    NormalizationTermApplicabilityRunNotCompletedError,
    NormalizationTermApplicabilityRunNotFoundError,
    NormalizationValueError,
)
from maximor.normalization.repository import NormalizationRepository
from maximor.normalization.normalizers import normalize_currency, normalize_date, normalize_money, normalize_period_label, normalize_quantity
from maximor.normalization.service import assemble_normalized_draft, assemble_normalized_line_item
from maximor.normalization.validation import ValidationReport, validate_normalized_extraction
from maximor.normalization.finalization import assess_finalization_readiness, build_finalization_candidate
from maximor.normalization.schemas import (
    FieldProvenance,
    FinalizationIssue,
    FinalizationReadiness,
    FinalizationStatus,
    FinalOrderFormExtraction,
    MoneyValue,
    NormalizedLineItem,
    NormalizedOrderMetadata,
    PriceScheduleEntry,
    ProvenanceSourceType,
    ReviewIssue,
    ReviewIssueSeverity,
    ReviewQueueClassification,
    ValidationStatus,
    ValueOrigin,
)

__all__ = [
    "FieldProvenance",
    "FinalOrderFormExtraction",
    "LineItemSourceBundle",
    "MoneyValue",
    "NormalizationAnalysisRunNotCompletedError",
    "NormalizationAnalysisRunNotFoundError",
    "NormalizationBundleConstructionError",
    "NormalizationCommercialFactCoverageMissingError",
    "NormalizationDocumentNotFoundError",
    "NormalizationError",
    "NormalizationInput",
    "NormalizationInputConstructionError",
    "NormalizationOrganizationNotFoundError",
    "NormalizationRepository",
    "NormalizationRequest",
    "NormalizationSkuMappingMissingError",
    "NormalizationSourceError",
    "NormalizationSourceMismatchError",
    "NormalizationTermApplicabilityRunNotCompletedError",
    "NormalizationTermApplicabilityRunNotFoundError",
    "NormalizationValueError",
    "NormalizedLineItem",
    "NormalizedOrderMetadata",
    "PriceScheduleEntry",
    "ProvenanceSourceType",
    "ReviewIssue",
    "ReviewIssueSeverity",
    "ValidationStatus",
    "ValueOrigin",
    "normalize_currency",
    "normalize_date",
    "normalize_money",
    "normalize_period_label",
    "normalize_quantity",
    "assemble_normalized_draft",
    "assemble_normalized_line_item",
    "ValidationReport",
    "validate_normalized_extraction",
    "FinalizationIssue",
    "FinalizationReadiness",
    "FinalizationStatus",
    "ReviewQueueClassification",
    "assess_finalization_readiness",
    "build_finalization_candidate",
    "assemble_line_item_source_bundles",
    "assemble_normalization_input",
    "eligible_candidate_ids_of",
    "expected_fact_fields",
    "status_by_candidate_map",
]
