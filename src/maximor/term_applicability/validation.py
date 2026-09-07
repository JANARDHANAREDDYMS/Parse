"""Deterministically validate a proposed term-applicability result before acceptance.

This module receives a trusted task and a proposed result, returns safe
structured issues, and does not call Claude, access a database, or persist
anything. It decides only whether a result is safe to accept as complete —
never whether a particular applicability judgement is the "right" one.

There is no check here for "did the decision create or change a SKU mapping,
commercial status, money, date, quantity, or term" because none of those are
representable at all in `TermApplicabilityDecision`/`TermApplicabilityResult` —
the schema itself is the enforcement, not a runtime check.

`unknown` scope is never flagged as an issue by this validator, at any
confidence level: rejecting a claimed `candidate` attribution the runtime
cannot support (see the retrieval-grounding checks below) is exactly how this
gate makes `unknown` the safe fallback in practice, without ever needing to
judge whether `unknown` itself was the "right" call.
"""

from dataclasses import dataclass
from typing import Protocol

from maximor.term_applicability.contracts import FACT_ELIGIBLE_COMMERCIAL_STATUSES, TermApplicabilityTask
from maximor.term_applicability.schemas import TermApplicabilityResult, TermDisposition
from maximor.term_applicability.versions import TERM_APPLICABILITY_RESULT_SCHEMA_VERSION


@dataclass(frozen=True)
class TermApplicabilityValidationIssue:
    """Describe one safe output defect and whether a narrow correction may address it."""

    code: str
    location: str
    message: str
    correctable: bool


class TermApplicabilityCompletionRuntime(Protocol):
    """Expose only successful tool-call facts needed by the completion gate.

    `retrieved_term_evidence_ids`/`retrieved_candidate_evidence_ids` hold,
    per term/candidate ID, the set of evidence IDs this invocation actually
    confirmed through a successful `get_term_evidence_region`/
    `get_candidate_evidence_region` call — not merely IDs the compact task
    happened to list. Validation cross-checks decision evidence against these
    captured facts instead of trusting that an ID which merely exists in the
    task was ever actually looked at, the same "trust a successful narrow-tool
    execution, don't assume" pattern `sku_mapping`'s completion gate uses for
    `authoritative_skus_by_id`.
    """

    retrieved_term_evidence_ids: dict[str, frozenset[str]]
    retrieved_candidate_evidence_ids: dict[str, frozenset[str]]


def validate_term_applicability_result(
    task: TermApplicabilityTask,
    result: TermApplicabilityResult,
    runtime: TermApplicabilityCompletionRuntime | None = None,
) -> tuple[TermApplicabilityValidationIssue, ...]:
    """Check trusted identity, task membership, evidence grounding, and retrieval completion."""

    issues: list[TermApplicabilityValidationIssue] = []
    retrieved_term_evidence = runtime.retrieved_term_evidence_ids if runtime is not None else {}
    retrieved_candidate_evidence = runtime.retrieved_candidate_evidence_ids if runtime is not None else {}

    if (
        result.organization_id, result.document_id, result.preprocessing_run_id, result.analysis_run_id,
    ) != (
        task.organization_id, task.document_id, task.preprocessing_run_id, task.analysis_run_id,
    ):
        issues.append(TermApplicabilityValidationIssue(
            "identity_mismatch", "result", "Result identifiers do not match the trusted task.", False,
        ))

    if result.schema_version != TERM_APPLICABILITY_RESULT_SCHEMA_VERSION:
        issues.append(TermApplicabilityValidationIssue(
            "version_mismatch", "result", "Result schema version does not match the expected version.", False,
        ))

    known_term_ids = set(task.term_ids)
    selected_term_ids = set(task.effective_selected_term_ids)
    known_candidate_ids = set(task.candidate_ids)
    decision_counts: dict[str, int] = {}

    for index, decision in enumerate(result.decisions):
        location = f"decisions.{index}"
        decision_counts[decision.term_id] = decision_counts.get(decision.term_id, 0) + 1

        if decision.term_id not in known_term_ids:
            issues.append(TermApplicabilityValidationIssue(
                "unknown_term", f"{location}.term_id", "A decision references a term outside the loaded task.", False,
            ))
            continue

        if decision.term_id not in selected_term_ids:
            issues.append(TermApplicabilityValidationIssue(
                "term_not_selected", f"{location}.term_id",
                "A decision references a term not selected for attribution.", True,
            ))
            continue

        unknown_candidates = [cid for cid in decision.applies_to_candidate_ids if cid not in known_candidate_ids]
        if unknown_candidates:
            issues.append(TermApplicabilityValidationIssue(
                "unknown_candidate", f"{location}.applies_to_candidate_ids",
                "A decision references a candidate outside the loaded task.", False,
            ))

        for reference in decision.evidence:
            evidence_id = reference.block_id or reference.table_id
            trusted = task.evidence_by_id.get(evidence_id)
            if trusted is None or trusted != reference:
                issues.append(TermApplicabilityValidationIssue(
                    "evidence_not_grounded_in_task", f"{location}.evidence",
                    "Decision evidence must resolve to the task's own trusted evidence.", True,
                ))
                break

        if decision.disposition is TermDisposition.LINE_ITEM:
            confirmed_for_term = retrieved_term_evidence.get(decision.term_id, frozenset())
            ungrounded = [
                (reference.block_id or reference.table_id)
                for reference in decision.evidence
                if (reference.block_id or reference.table_id) not in confirmed_for_term
            ]
            if ungrounded:
                issues.append(TermApplicabilityValidationIssue(
                    "evidence_not_retrieved", f"{location}.evidence",
                    "A line-item decision's evidence must be confirmed by a successful evidence retrieval this run.", True,
                ))

            if not unknown_candidates:
                for candidate_id in decision.applies_to_candidate_ids:
                    if not retrieved_candidate_evidence.get(candidate_id):
                        issues.append(TermApplicabilityValidationIssue(
                            "candidate_evidence_not_retrieved", f"{location}.applies_to_candidate_ids",
                            "A candidate-scope decision requires a successful candidate-evidence retrieval for each named candidate.", True,
                        ))
                        break

    candidates_by_id = task.candidates_by_id
    seen_fact_ids: set[str] = set()
    for bundle_index, bundle in enumerate(result.candidate_commercial_facts):
        bundle_location = f"candidate_commercial_facts.{bundle_index}"
        candidate = candidates_by_id.get(bundle.candidate_id)

        if candidate is None:
            issues.append(TermApplicabilityValidationIssue(
                "fact_unknown_candidate", f"{bundle_location}.candidate_id",
                "A commercial-fact bundle references a candidate outside the loaded task.", False,
            ))
            continue

        if candidate.commercial_status not in FACT_ELIGIBLE_COMMERCIAL_STATUSES:
            issues.append(TermApplicabilityValidationIssue(
                "fact_candidate_not_eligible", f"{bundle_location}.candidate_id",
                "Commercial facts may only be reported for purchased/included candidates.", True,
            ))
            continue

        confirmed_for_candidate = retrieved_candidate_evidence.get(bundle.candidate_id, frozenset())
        seen_period_contexts: dict[tuple, int] = {}

        for fact_index, fact in enumerate(bundle.facts):
            fact_location = f"{bundle_location}.facts.{fact_index}"

            if fact.fact_id in seen_fact_ids:
                issues.append(TermApplicabilityValidationIssue(
                    "duplicate_fact_id", f"{fact_location}.fact_id",
                    "A fact_id must be unique across the entire result.", True,
                ))
            seen_fact_ids.add(fact.fact_id)

            period_context = (fact.field, fact.raw_period_label, fact.raw_period_start, fact.raw_period_end)
            if period_context in seen_period_contexts:
                issues.append(TermApplicabilityValidationIssue(
                    "duplicate_fact_period", f"{fact_location}.raw_period_label",
                    "A candidate may not report more than one fact for the same field and period context.", True,
                ))
            seen_period_contexts[period_context] = fact_index

            for reference in fact.evidence:
                evidence_id = reference.block_id or reference.table_id
                trusted = task.evidence_by_id.get(evidence_id)
                if trusted is None or trusted != reference:
                    issues.append(TermApplicabilityValidationIssue(
                        "fact_evidence_not_grounded_in_task", f"{fact_location}.evidence",
                        "Fact evidence must resolve to the task's own trusted evidence.", True,
                    ))
                    break

            ungrounded = [
                (reference.block_id or reference.table_id)
                for reference in fact.evidence
                if (reference.block_id or reference.table_id) not in confirmed_for_candidate
            ]
            if ungrounded:
                issues.append(TermApplicabilityValidationIssue(
                    "fact_evidence_not_retrieved", f"{fact_location}.evidence",
                    "A commercial fact's evidence must be confirmed by a successful candidate-evidence retrieval this run.", True,
                ))

    # A selected subset is explicit after triage.  ``None`` retains the
    # pre-triage/legacy task contract, where fact-only submissions remain
    # compatible and no coverage completeness claim is implied.
    required_term_ids = task.effective_selected_term_ids if task.selected_term_ids is not None else ()
    for term_id in required_term_ids:
        if decision_counts.get(term_id, 0) == 0:
            issues.append(TermApplicabilityValidationIssue(
                "selected_term_decision_missing", f"decisions[{term_id}]",
                "Every selected term requires exactly one applicability decision.", True,
            ))

    expected_by_candidate = task.expected_fact_fields_by_candidate
    coverage_by_candidate = {item.candidate_id: item for item in result.candidate_commercial_fact_coverage}
    for candidate_id, expected_fields in expected_by_candidate.items():
        coverage = coverage_by_candidate.get(candidate_id)
        if coverage is None:
            issues.append(TermApplicabilityValidationIssue(
                "eligible_candidate_fact_coverage_missing", f"candidate_commercial_fact_coverage[{candidate_id}]",
                "An eligible candidate with raw fact hints requires a coverage declaration.", True,
            ))
            continue
        if tuple(coverage.expected_fields) != tuple(expected_fields):
            issues.append(TermApplicabilityValidationIssue(
                "candidate_fact_coverage_expected_fields_mismatch", f"candidate_commercial_fact_coverage[{candidate_id}].expected_fields",
                "Coverage expected fields must match application-derived hints.", False,
            ))
        # `extracted_fields` is never Claude's own summary on the live path
        # (the finalizer derives it server-side from this same submission's
        # own `candidate_commercial_facts` -- see
        # `ClaudeTermApplicabilityAgent._resolve_coverage_evidence_ids` --
        # so it can no longer drift from the facts actually submitted). This
        # check remains as a structural invariant of the canonical result
        # itself, covering any other caller that constructs one directly.
        extracted = {fact.field for bundle in result.candidate_commercial_facts if bundle.candidate_id == candidate_id for fact in bundle.facts}
        if set(extracted) != set(coverage.extracted_fields):
            issues.append(TermApplicabilityValidationIssue(
                "candidate_fact_coverage_extracted_mismatch", f"candidate_commercial_fact_coverage[{candidate_id}].extracted_fields",
                "Coverage extracted fields must match persisted facts.", False,
            ))
        accounted = set(coverage.extracted_fields) | set(coverage.unresolved_fields)
        if set(expected_fields) - accounted:
            issues.append(TermApplicabilityValidationIssue(
                "candidate_fact_field_unresolved", f"candidate_commercial_fact_coverage[{candidate_id}]",
                "Every expected field must be extracted or explicitly unresolved.", True,
            ))
        if coverage.unresolved_fields and not retrieved_candidate_evidence.get(candidate_id):
            issues.append(TermApplicabilityValidationIssue(
                "candidate_fact_unresolved_evidence_not_retrieved", f"candidate_commercial_fact_coverage[{candidate_id}].evidence",
                "Unresolved fields require successfully retrieved candidate evidence.", True,
            ))
        for reference in coverage.evidence:
            evidence_id = reference.block_id or reference.table_id
            if task.evidence_by_id.get(evidence_id) != reference or evidence_id not in retrieved_candidate_evidence.get(candidate_id, frozenset()):
                issues.append(TermApplicabilityValidationIssue(
                    "candidate_fact_coverage_evidence_not_retrieved", f"candidate_commercial_fact_coverage[{candidate_id}].evidence",
                    "Coverage evidence must be trusted candidate evidence retrieved this run.", True,
                ))
                break

    for coverage in result.candidate_commercial_fact_coverage:
        if coverage.candidate_id not in expected_by_candidate:
            candidate = candidates_by_id.get(coverage.candidate_id)
            code = "candidate_fact_coverage_candidate_not_eligible" if candidate is not None else "candidate_fact_coverage_unknown_candidate"
            issues.append(TermApplicabilityValidationIssue(
                code, f"candidate_commercial_fact_coverage[{coverage.candidate_id}]",
                "Coverage is not allowed for this candidate.", False,
            ))

    return tuple(issues)
