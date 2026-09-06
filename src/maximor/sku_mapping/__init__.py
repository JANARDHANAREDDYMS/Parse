"""Expose SKU-mapping foundation contracts, interfaces, and the demo retriever.

Imports perform no database access, no catalog access, and no Claude call. A
caller explicitly constructs and invokes a concrete repository/retriever/agent.
"""

from maximor.sku_mapping.agent import (
    ClaudeSkuMappingAgent,
    SkuMappingAgent,
    SkuMappingExecution,
    SkuMappingRuntimeSummary,
    UnconfiguredSkuMappingAgent,
)
from maximor.sku_mapping.contracts import (
    EvidenceResolver,
    HybridSkuRetriever,
    SemanticSkuSource,
    SkuMappingTask,
    SkuMappingToolset,
    SkuRepository,
    build_sku_mapping_task,
)
from maximor.sku_mapping.errors import (
    ActiveCatalogVersionNotFoundError,
    AuthoritativeLookupBeforeRetrievalError,
    InvalidRetrievalLimitError,
    SkuMappingConfigurationError,
    SkuMappingError,
    SkuMappingNotConfiguredError,
    SkuMappingRuntimeError,
    SkuMappingTaskConstructionError,
    SkuMappingToolError,
    SkuMappingValidationError,
    SkuNotFoundError,
)
from maximor.sku_mapping.repository import PostgresSkuRepository
from maximor.sku_mapping.retrieval import DeterministicHybridSkuRetriever
from maximor.sku_mapping.schemas import (
    CatalogVersionRecord,
    RetrievedSku,
    SkuMappingDecision,
    SkuMappingOutcome,
    SkuMatchSource,
    SkuRecord,
    SkuRetrievalResult,
)
from maximor.sku_mapping.tool_schemas import GetAuthoritativeSkuInput, RetrieveSkusInput
from maximor.sku_mapping.tools import PersistedSkuMappingTools
from maximor.sku_mapping.validation import (
    SkuMappingCompletionRuntime,
    SkuMappingValidationIssue,
    validate_sku_mapping_decision,
)

__all__ = [
    "ActiveCatalogVersionNotFoundError",
    "AuthoritativeLookupBeforeRetrievalError",
    "build_sku_mapping_task",
    "CatalogVersionRecord",
    "ClaudeSkuMappingAgent",
    "DeterministicHybridSkuRetriever",
    "EvidenceResolver",
    "GetAuthoritativeSkuInput",
    "HybridSkuRetriever",
    "InvalidRetrievalLimitError",
    "PersistedSkuMappingTools",
    "PostgresSkuRepository",
    "RetrievedSku",
    "RetrieveSkusInput",
    "SemanticSkuSource",
    "SkuMappingAgent",
    "SkuMappingCompletionRuntime",
    "SkuMappingConfigurationError",
    "SkuMappingDecision",
    "SkuMappingError",
    "SkuMappingExecution",
    "SkuMappingNotConfiguredError",
    "SkuMappingOutcome",
    "SkuMappingRuntimeError",
    "SkuMappingRuntimeSummary",
    "SkuMappingTask",
    "SkuMappingTaskConstructionError",
    "SkuMappingToolError",
    "SkuMappingToolset",
    "SkuMappingValidationError",
    "SkuMappingValidationIssue",
    "SkuMatchSource",
    "SkuNotFoundError",
    "SkuRecord",
    "SkuRepository",
    "SkuRetrievalResult",
    "UnconfiguredSkuMappingAgent",
    "validate_sku_mapping_decision",
]
