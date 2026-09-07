"""Define safe failures for the term-applicability foundation and its future agent.

These errors carry no document content, no term/candidate text, and no SQL.
They report only stable safe codes and messages, matching the sibling
`document_analysis.errors`/`sku_mapping.errors` convention.
"""


class TermApplicabilityError(Exception):
    """Carry a stable safe code and message for term-applicability callers."""

    def __init__(self, code: str, safe_message: str) -> None:
        """Create a safe error without paths, content, credentials, or tracebacks."""

        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class TermApplicabilityTaskConstructionError(TermApplicabilityError):
    """Report that a `TermApplicabilityTask` cannot be built from the supplied analysis result."""


class TermApplicabilityResultConstructionError(TermApplicabilityError):
    """Report that a proposed result references a term or candidate outside its task."""


class TermApplicabilityNotConfiguredError(TermApplicabilityError):
    """Report that no concrete term-applicability agent has been configured."""

    def __init__(self) -> None:
        """Create the explicit no-fake-success placeholder failure."""

        super().__init__(
            "term_applicability_not_configured",
            "Term applicability is not configured.",
        )


class TermApplicabilityConfigurationError(TermApplicabilityError):
    """Report missing safe agent configuration without disclosing secrets."""

    def __init__(self) -> None:
        super().__init__("term_applicability_not_configured", "Term-applicability configuration is unavailable.")


class TermApplicabilitySourceError(TermApplicabilityError):
    """Base class for safe repository-level source-loading failures."""


class TermApplicabilityAnalysisRunNotFoundError(TermApplicabilitySourceError):
    """Report a missing or cross-tenant document-analysis run."""

    def __init__(self) -> None:
        super().__init__("term_applicability_analysis_run_not_found", "The requested document-analysis run is unavailable.")


class TermApplicabilityAnalysisRunNotCompletedError(TermApplicabilitySourceError):
    """Report a document-analysis run that has not reached completed status."""

    def __init__(self) -> None:
        super().__init__("term_applicability_analysis_run_not_completed", "The document-analysis run is not completed.")


class TermApplicabilitySourceMismatchError(TermApplicabilitySourceError):
    """Report a canonical result whose identity does not match the requested source."""

    def __init__(self) -> None:
        super().__init__("term_applicability_source_mismatch", "The document-analysis result does not match the requested source.")


class TermApplicabilityToolError(TermApplicabilityError):
    """Base class for safe narrow-tool failures raised by `PersistedTermApplicabilityTools`."""


class TaskScopeMismatchError(TermApplicabilityToolError):
    """Report a tool request whose organization/analysis-run scope does not match the fixed task."""

    def __init__(self) -> None:
        super().__init__("term_applicability_task_scope_mismatch", "The request scope does not match this task.")


class TermNotFoundError(TermApplicabilityToolError):
    """Report that the requested term does not belong to this task."""

    def __init__(self) -> None:
        super().__init__("term_applicability_term_not_found", "The requested term is unavailable in this task.")


class CandidateNotFoundError(TermApplicabilityToolError):
    """Report that the requested candidate does not belong to this task."""

    def __init__(self) -> None:
        super().__init__("term_applicability_candidate_not_found", "The requested candidate is unavailable in this task.")


class EvidenceIdNotFoundError(TermApplicabilityToolError):
    """Report that the requested evidence ID does not belong to the named term/candidate."""

    def __init__(self) -> None:
        super().__init__("term_applicability_evidence_id_not_found", "The requested evidence ID is unavailable for this term or candidate.")


class TermApplicabilityRuntimeError(TermApplicabilityError):
    """Report a bounded SDK execution failure safely."""

    def __init__(self, code: str = "term_applicability_runtime_failed", *, runtime: object | None = None) -> None:
        """Attach optional bounded runtime diagnostics without exposing SDK details."""

        super().__init__(code, "Term applicability could not be completed.")
        self.runtime = runtime


class TermApplicabilityValidationError(TermApplicabilityError):
    """Report invalid or missing structured agent output without returning it."""

    def __init__(self, *, runtime: object | None = None) -> None:
        """Retain optional bounded diagnostics when output validation fails."""

        super().__init__("term_applicability_invalid_output", "Term applicability returned an invalid result.")
        self.runtime = runtime


class TermApplicabilityPersistenceError(TermApplicabilityError):
    """Report a safe combined-run relational-persistence failure.

    Used only by `TermApplicabilityPersistenceService.mark_run_failed` — the
    canonical-artifact `save_completed_result`/`load_completed_result` paths
    keep their existing bare `ValueError` shape so their own established
    callers and tests are unaffected; this class exists so the best-effort
    failure-recording path itself can fail safely and typed, matching the
    sibling `DocumentAnalysisPersistenceError`/`SkuMappingPersistenceError`
    convention.
    """

    def __init__(self, code: str = "term_applicability_persistence_failed") -> None:
        """Create a stable persistence error without source content or internal details."""
        super().__init__(code, "Term applicability persistence could not be completed.")
