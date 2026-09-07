"""Define safe failures for trusted-input assembly in the normalization foundation.

These errors carry no document content, no term/candidate/fact text, no raw
money or date values, and no SQL. They report only stable safe codes and
messages, matching the sibling `document_analysis.errors`/`sku_mapping.errors`/
`term_applicability.errors` convention.
"""


class NormalizationError(Exception):
    """Carry a stable safe code and message for normalization callers."""

    def __init__(self, code: str, safe_message: str) -> None:
        """Create a safe error without paths, content, credentials, or tracebacks."""

        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class NormalizationSourceError(NormalizationError):
    """Base class for safe repository-level source-loading failures."""


class NormalizationOrganizationNotFoundError(NormalizationSourceError):
    """Report a missing tenant organization."""

    def __init__(self) -> None:
        super().__init__("normalization_organization_not_found", "The organization is unavailable.")


class NormalizationDocumentNotFoundError(NormalizationSourceError):
    """Report a missing or cross-tenant document."""

    def __init__(self) -> None:
        super().__init__("normalization_document_not_found", "The document is unavailable.")


class NormalizationAnalysisRunNotFoundError(NormalizationSourceError):
    """Report a missing or cross-tenant document-analysis run."""

    def __init__(self) -> None:
        super().__init__("normalization_analysis_run_not_found", "The requested document-analysis run is unavailable.")


class NormalizationAnalysisRunNotCompletedError(NormalizationSourceError):
    """Report a document-analysis run that has not reached completed status."""

    def __init__(self) -> None:
        super().__init__("normalization_analysis_run_not_completed", "The document-analysis run is not completed.")


class NormalizationTermApplicabilityRunNotFoundError(NormalizationSourceError):
    """Report a missing or cross-tenant term-applicability run."""

    def __init__(self) -> None:
        super().__init__(
            "normalization_term_applicability_run_not_found",
            "The requested term-applicability run is unavailable.",
        )


class NormalizationTermApplicabilityRunNotCompletedError(NormalizationSourceError):
    """Report a term-applicability run that has not reached completed status."""

    def __init__(self) -> None:
        super().__init__(
            "normalization_term_applicability_run_not_completed",
            "The term-applicability run is not completed.",
        )


class NormalizationSourceMismatchError(NormalizationSourceError):
    """Report loaded sources whose organization/document/analysis-run identity disagree."""

    def __init__(self) -> None:
        super().__init__("normalization_source_mismatch", "The loaded sources do not share the same identity.")


class NormalizationSkuMappingMissingError(NormalizationSourceError):
    """Report a purchased/included candidate with no completed MATCH SKU mapping.

    Raised rather than silently skipping the candidate -- an eligible
    candidate expected to become a line item must never disappear from
    assembly without an explicit, typed reason.
    """

    def __init__(self) -> None:
        super().__init__(
            "normalization_sku_mapping_missing",
            "An eligible candidate has no completed matching SKU mapping.",
        )


class NormalizationCommercialFactCoverageMissingError(NormalizationSourceError):
    """Report a purchased/included candidate with no completed commercial-fact coverage.

    Raised rather than silently skipping the candidate, mirroring
    `NormalizationSkuMappingMissingError`.
    """

    def __init__(self) -> None:
        super().__init__(
            "normalization_commercial_fact_coverage_missing",
            "An eligible candidate has no completed commercial-fact coverage.",
        )


class NormalizationInputConstructionError(NormalizationError):
    """Report that a `NormalizationInput` cannot be built from the supplied sources."""


class NormalizationBundleConstructionError(NormalizationError):
    """Report that a `LineItemSourceBundle` cannot be built from a `NormalizationInput`."""


class NormalizationValueError(NormalizationError):
    """Report a bounded primitive-format or deterministic derivation issue."""
