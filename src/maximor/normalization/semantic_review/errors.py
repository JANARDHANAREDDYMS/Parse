"""Safe semantic-review failures without source content or infrastructure details."""


class NormalizationSemanticReviewError(Exception):
    """Carry a stable safe error code and message."""

    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class SemanticReviewTaskError(NormalizationSemanticReviewError):
    """Reject an unsafe or ineligible review task."""


class SemanticReviewValidationError(NormalizationSemanticReviewError):
    """Reject a proposed review result."""

    def __init__(self, code="normalization_semantic_review_invalid_output", safe_message="Normalization semantic review returned an invalid result.", *, runtime=None):
        super().__init__(code, safe_message)
        self.runtime = runtime


class SemanticReviewRuntimeError(NormalizationSemanticReviewError):
    """Report bounded SDK execution failure."""

    def __init__(self, code="normalization_semantic_review_runtime_failed", *, runtime=None):
        super().__init__(code, "Normalization semantic review could not be completed.")
        self.runtime = runtime


class SemanticReviewConfigurationError(NormalizationSemanticReviewError):
    """Report unavailable agent configuration without exposing secrets."""

    def __init__(self):
        super().__init__("normalization_semantic_review_not_configured", "Normalization semantic review is not configured.")
