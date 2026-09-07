"""Expose term-applicability contracts, interfaces, tools, and the Claude agent.

Imports perform no database access and no Claude call by themselves. A
caller explicitly constructs and invokes `TermApplicabilityRepository`,
`PersistedTermApplicabilityTools`, and `ClaudeTermApplicabilityAgent`.
"""

from maximor.term_applicability.agent import (
    ClaudeTermApplicabilityAgent,
    TermApplicabilityAgent,
    TermApplicabilityExecution,
    TermApplicabilityRuntimeSummary,
    UnconfiguredTermApplicabilityAgent,
)
from maximor.term_applicability.contracts import (
    FACT_ELIGIBLE_COMMERCIAL_STATUSES,
    CandidateContext,
    EvidenceResolver,
    TermApplicabilityTask,
    TermApplicabilityToolset,
    TermContext,
    build_selected_term_applicability_task,
    build_term_applicability_result,
    build_term_applicability_task,
)
from maximor.term_applicability.errors import (
    TermApplicabilityAnalysisRunNotCompletedError,
    TermApplicabilityAnalysisRunNotFoundError,
    TermApplicabilityConfigurationError,
    TermApplicabilityError,
    TermApplicabilityNotConfiguredError,
    TermApplicabilityResultConstructionError,
    TermApplicabilityRuntimeError,
    TermApplicabilitySourceMismatchError,
    TermApplicabilityTaskConstructionError,
    TermApplicabilityToolError,
    TermApplicabilityValidationError,
)
from maximor.term_applicability.repository import TermApplicabilityRepository
from maximor.term_applicability.schemas import (
    CandidateCommercialFacts,
    CandidateCommercialFactCoverage,
    RawCommercialFact,
    RawCommercialFactField,
    TermApplicabilityDecision,
    TermApplicabilityResult,
    TermDisposition,
    compute_fact_id,
)
from maximor.term_applicability.tool_schemas import CandidateEvidenceRegionInput, TermEvidenceRegionInput
from maximor.term_applicability.tools import PersistedTermApplicabilityTools
from maximor.term_applicability.validation import (
    TermApplicabilityCompletionRuntime,
    TermApplicabilityValidationIssue,
    validate_term_applicability_result,
)

__all__ = [
    "FACT_ELIGIBLE_COMMERCIAL_STATUSES",
    "build_selected_term_applicability_task",
    "build_term_applicability_result",
    "build_term_applicability_task",
    "compute_fact_id",
    "CandidateCommercialFacts",
    "CandidateCommercialFactCoverage",
    "CandidateContext",
    "CandidateEvidenceRegionInput",
    "ClaudeTermApplicabilityAgent",
    "EvidenceResolver",
    "PersistedTermApplicabilityTools",
    "RawCommercialFact",
    "RawCommercialFactField",
    "TermApplicabilityAgent",
    "TermApplicabilityAnalysisRunNotCompletedError",
    "TermApplicabilityAnalysisRunNotFoundError",
    "TermApplicabilityCompletionRuntime",
    "TermApplicabilityConfigurationError",
    "TermApplicabilityDecision",
    "TermApplicabilityError",
    "TermApplicabilityExecution",
    "TermApplicabilityNotConfiguredError",
    "TermApplicabilityRepository",
    "TermApplicabilityResult",
    "TermApplicabilityResultConstructionError",
    "TermApplicabilityRuntimeError",
    "TermApplicabilityRuntimeSummary",
    "TermApplicabilitySourceMismatchError",
    "TermApplicabilityTask",
    "TermApplicabilityTaskConstructionError",
    "TermApplicabilityToolError",
    "TermApplicabilityToolset",
    "TermApplicabilityValidationError",
    "TermApplicabilityValidationIssue",
    "TermContext",
    "TermDisposition",
    "TermEvidenceRegionInput",
    "UnconfiguredTermApplicabilityAgent",
    "validate_term_applicability_result",
]
