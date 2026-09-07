"""Define safe failures for term triage and its Claude agent.

These errors carry no document content, no term text, and no SQL. They
report only stable safe codes and messages, matching the sibling
`document_analysis.errors`/`sku_mapping.errors`/`term_applicability.errors`
convention.
"""


class TermTriageError(Exception):
    """Carry a stable safe code and message for term-triage callers."""

    def __init__(self, code: str, safe_message: str) -> None:
        """Create a safe error without paths, content, credentials, or tracebacks."""

        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class TermTriageTaskConstructionError(TermTriageError):
    """Report that a `TermTriageTask` cannot be built from the supplied analysis result."""


class TermTriageResultConstructionError(TermTriageError):
    """Report that a proposed result does not exactly cover its task's terms."""


class TermTriageNotConfiguredError(TermTriageError):
    """Report that no concrete term-triage agent has been configured."""

    def __init__(self) -> None:
        """Create the explicit no-fake-success placeholder failure."""

        super().__init__("term_triage_not_configured", "Term triage is not configured.")


class TermTriageConfigurationError(TermTriageError):
    """Report missing safe agent configuration without disclosing secrets."""

    def __init__(self) -> None:
        super().__init__("term_triage_not_configured", "Term-triage configuration is unavailable.")


class TermTriageRuntimeError(TermTriageError):
    """Report a bounded SDK execution failure safely."""

    def __init__(self, code: str = "term_triage_runtime_failed", *, runtime: object | None = None) -> None:
        """Attach optional bounded runtime diagnostics without exposing SDK details."""

        super().__init__(code, "Term triage could not be completed.")
        self.runtime = runtime


class TermTriageValidationError(TermTriageError):
    """Report invalid or missing structured agent output without returning it."""

    def __init__(self, *, runtime: object | None = None) -> None:
        """Retain optional bounded diagnostics when output validation fails."""

        super().__init__("term_triage_invalid_output", "Term triage returned an invalid result.")
        self.runtime = runtime
