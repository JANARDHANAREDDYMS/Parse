"""Expose term-triage contracts, interfaces, and the Claude agent.

Imports perform no database access and no Claude call by themselves. A
caller explicitly constructs and invokes `ClaudeTermTriageAgent`.
"""

from maximor.term_triage.agent import (
    ClaudeTermTriageAgent,
    TermTriageAgent,
    TermTriageExecution,
    TermTriageRuntimeSummary,
    UnconfiguredTermTriageAgent,
)
from maximor.term_triage.contracts import (
    TermTriageTask,
    TriageCandidateContext,
    TriageTermContext,
    build_term_triage_result,
    build_term_triage_task,
)
from maximor.term_triage.errors import (
    TermTriageConfigurationError,
    TermTriageError,
    TermTriageNotConfiguredError,
    TermTriageResultConstructionError,
    TermTriageRuntimeError,
    TermTriageTaskConstructionError,
    TermTriageValidationError,
)
from maximor.term_triage.schemas import TermTriageDecision, TermTriageDisposition, TermTriageResult
from maximor.term_triage.validation import TermTriageValidationIssue, validate_term_triage_result

__all__ = [
    "build_term_triage_result",
    "build_term_triage_task",
    "ClaudeTermTriageAgent",
    "TermTriageAgent",
    "TermTriageConfigurationError",
    "TermTriageDecision",
    "TermTriageDisposition",
    "TermTriageError",
    "TermTriageExecution",
    "TermTriageNotConfiguredError",
    "TermTriageResult",
    "TermTriageResultConstructionError",
    "TermTriageRuntimeError",
    "TermTriageRuntimeSummary",
    "TermTriageTask",
    "TermTriageTaskConstructionError",
    "TermTriageValidationError",
    "TermTriageValidationIssue",
    "TriageCandidateContext",
    "TriageTermContext",
    "UnconfiguredTermTriageAgent",
    "validate_term_triage_result",
]
