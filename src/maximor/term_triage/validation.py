"""Deterministically validate a proposed term-triage result before acceptance.

This module receives a trusted task and a proposed result, returns safe
structured issues, and does not call Claude, access a database, or persist
anything. It decides only whether a result is safe to accept as complete --
never whether a particular disposition is the "right" one. Coverage
completeness (exactly one decision per task term) is the central rule here,
since a partial decisions list would still satisfy `TermTriageResult`'s own
schema-level validator.
"""

from dataclasses import dataclass

from maximor.term_triage.contracts import TermTriageTask
from maximor.term_triage.schemas import TermTriageResult
from maximor.term_triage.versions import TERM_TRIAGE_RESULT_SCHEMA_VERSION


@dataclass(frozen=True)
class TermTriageValidationIssue:
    """Describe one safe output defect and whether a narrow correction may address it."""

    code: str
    location: str
    message: str
    correctable: bool


def validate_term_triage_result(
    task: TermTriageTask, result: TermTriageResult,
) -> tuple[TermTriageValidationIssue, ...]:
    """Check trusted identity, version, and exact term coverage."""

    issues: list[TermTriageValidationIssue] = []

    if (
        result.organization_id, result.document_id, result.preprocessing_run_id, result.analysis_run_id,
    ) != (
        task.organization_id, task.document_id, task.preprocessing_run_id, task.analysis_run_id,
    ):
        issues.append(TermTriageValidationIssue(
            "identity_mismatch", "result", "Result identifiers do not match the trusted task.", False,
        ))

    if result.schema_version != TERM_TRIAGE_RESULT_SCHEMA_VERSION:
        issues.append(TermTriageValidationIssue(
            "version_mismatch", "result", "Result schema version does not match the expected version.", False,
        ))

    known_term_ids = set(task.term_ids)
    decided_term_ids = [decision.term_id for decision in result.decisions]

    unknown = sorted(set(decided_term_ids) - known_term_ids)
    if unknown:
        issues.append(TermTriageValidationIssue(
            "unknown_term", "result:decisions", "A decision references a term outside the loaded task.", True,
        ))

    missing = sorted(known_term_ids - set(decided_term_ids))
    if missing:
        issues.append(TermTriageValidationIssue(
            "incomplete_coverage", "result:decisions", "The triage result does not cover every term in the task.", True,
        ))

    return tuple(issues)
