"""Define safe failures for the SKU-mapping foundation and its Claude agent.

These errors carry no document content, no candidate text, and no SQL. They
report only stable safe codes and messages, matching the sibling
`document_analysis.errors` convention.
"""


class SkuMappingError(Exception):
    """Carry a stable safe code and message for SKU-mapping callers."""

    def __init__(self, code: str, safe_message: str) -> None:
        """Create a safe error without paths, content, credentials, or tracebacks."""

        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class SkuMappingTaskConstructionError(SkuMappingError):
    """Report that a `SkuMappingTask` cannot be built from the supplied analysis result."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(code, safe_message)


class SkuMappingToolError(SkuMappingError):
    """Base class for safe narrow-tool failures raised by `PersistedSkuMappingTools`."""


class ActiveCatalogVersionNotFoundError(SkuMappingToolError):
    """Report that a tenant has no active catalog version to retrieve against."""

    def __init__(self) -> None:
        super().__init__(
            "active_catalog_version_not_found",
            "The organization has no active SKU catalog version.",
        )


class InvalidRetrievalLimitError(SkuMappingToolError):
    """Report an out-of-bounds requested retrieval limit before any catalog access."""

    def __init__(self) -> None:
        super().__init__(
            "invalid_retrieval_limit",
            "The requested SKU retrieval limit is invalid.",
        )


class SkuNotFoundError(SkuMappingToolError):
    """Report that no active SKU matches the requested authoritative lookup."""

    def __init__(self) -> None:
        super().__init__(
            "sku_not_found",
            "The requested SKU was not found in the active catalog version.",
        )


class AuthoritativeLookupBeforeRetrievalError(SkuMappingToolError):
    """Report an authoritative lookup attempted before any retrieval in this invocation."""

    def __init__(self) -> None:
        super().__init__(
            "authoritative_lookup_before_retrieval",
            "An authoritative SKU lookup requires a prior retrieval in this invocation.",
        )


class SkuMappingNotConfiguredError(SkuMappingError):
    """Report that no concrete SKU-mapping agent has been configured."""

    def __init__(self) -> None:
        """Create the explicit no-fake-success placeholder failure."""

        super().__init__(
            "sku_mapping_not_configured",
            "SKU mapping is not configured.",
        )


class SkuMappingConfigurationError(SkuMappingError):
    """Report missing safe agent configuration without disclosing secrets."""

    def __init__(self) -> None:
        super().__init__("sku_mapping_not_configured", "SKU-mapping configuration is unavailable.")


class SkuMappingRuntimeError(SkuMappingError):
    """Report a bounded SDK execution failure safely."""

    def __init__(self, code: str = "sku_mapping_runtime_failed", *, runtime: object | None = None) -> None:
        """Attach optional bounded runtime diagnostics without exposing SDK details."""

        super().__init__(code, "SKU mapping could not be completed.")
        self.runtime = runtime


class SkuMappingValidationError(SkuMappingError):
    """Report invalid or missing structured agent output without returning it."""

    def __init__(self, *, runtime: object | None = None) -> None:
        """Retain optional bounded diagnostics when output validation fails."""

        super().__init__("sku_mapping_invalid_output", "SKU mapping returned an invalid decision.")
        self.runtime = runtime


class SkuMappingPersistenceError(SkuMappingError):
    """Report a safe canonical-artifact or relational-persistence failure."""

    def __init__(self, code: str = "sku_mapping_persistence_failed") -> None:
        """Create a stable persistence error without source content or internal details."""
        super().__init__(code, "SKU-mapping persistence could not be completed.")
