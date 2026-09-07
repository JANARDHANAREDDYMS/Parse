"""Define the versioned term-triage input contract and its deterministic builders.

`TermTriageTask` is built from one already-validated `DocumentAnalysisResult`
— every product candidate and every global term it contains, batched for one
completed analysis run, with only enough context for a coarse classification
pass. This module performs no database access, no evidence resolution, and
no Claude call.

Unlike `term_applicability.contracts.TermApplicabilityTask`, nothing here
carries evidence at all -- not even evidence IDs. `TermTriageAgent` has no
evidence tools and makes no evidence-grounded claim; it only judges each
term's rough shape from its own raw name/value and the candidate list.
"""

import uuid

from pydantic import Field, model_validator

from maximor.document_analysis.schemas import (
    MAX_GLOBAL_TERMS,
    MAX_PRODUCT_CANDIDATES,
    CommercialStatus,
    DocumentAnalysisResult,
    Identifier,
)
from maximor.term_triage.errors import TermTriageResultConstructionError, TermTriageTaskConstructionError
from maximor.term_triage.schemas import TermTriageDecision, TermTriageModel, TermTriageResult


class TriageCandidateContext(TermTriageModel):
    """Represent one product candidate's minimal facts for triage context only.

    No evidence, no raw attributes -- triage only needs enough to recognize
    that named candidates exist, not to reason about any one of them.
    """

    candidate_id: Identifier
    raw_name: str = Field(min_length=1, max_length=2_000)
    commercial_status: CommercialStatus


class TriageTermContext(TermTriageModel):
    """Represent one raw global term's minimal facts for triage classification."""

    term_id: Identifier
    raw_name: str = Field(min_length=1, max_length=500)
    raw_value: str | None = Field(default=None, max_length=4_000)


class TermTriageTask(TermTriageModel):
    """Carry every candidate and every term for one completed analysis run.

    This is a batch: one task covers an entire `document_analysis_run`, not
    one term -- `ClaudeTermTriageAgent` classifies the whole run's terms in
    one invocation.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    document_analysis_schema_version: str = Field(min_length=1, max_length=50)
    document_analysis_agent_version: str = Field(min_length=1, max_length=100)
    candidates: tuple[TriageCandidateContext, ...] = Field(default=(), max_length=MAX_PRODUCT_CANDIDATES)
    terms: tuple[TriageTermContext, ...] = Field(default=(), max_length=MAX_GLOBAL_TERMS)

    @property
    def term_ids(self) -> tuple[str, ...]:
        """Return every loaded term's identifier, for membership/coverage checks."""

        return tuple(item.term_id for item in self.terms)

    @model_validator(mode="after")
    def identifiers_are_unique_and_ordered(self) -> "TermTriageTask":
        """Require deterministic ascending IDs with no duplicates in either collection."""

        candidate_ids = [item.candidate_id for item in self.candidates]
        if candidate_ids != sorted(candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("task candidates must be unique and ordered by candidate_id")
        term_ids = list(self.term_ids)
        if term_ids != sorted(term_ids) or len(term_ids) != len(set(term_ids)):
            raise ValueError("task terms must be unique and ordered by term_id")
        return self


def build_term_triage_task(
    *,
    result: DocumentAnalysisResult,
    analysis_run_id: uuid.UUID,
    schema_version: str,
) -> TermTriageTask:
    """Build one batch task from every candidate and term in an already-validated result.

    Requires exactly one commercial-status assessment linked to each product
    candidate, mirroring `term_applicability.contracts.build_term_applicability_task`'s
    identical check on the same underlying data: a candidate with zero or
    more than one linked assessment is an unresolved upstream modelling
    ambiguity, not something either batch construction can silently paper
    over.
    """

    candidates: list[TriageCandidateContext] = []
    for candidate in result.product_candidates:
        linked = [item for item in result.commercial_statuses if item.candidate_id == candidate.candidate_id]
        if len(linked) == 0:
            raise TermTriageTaskConstructionError(
                "term_triage_status_missing",
                "A product candidate has no linked commercial-status assessment.",
            )
        if len(linked) > 1:
            raise TermTriageTaskConstructionError(
                "term_triage_status_ambiguous",
                "A product candidate has more than one linked commercial-status assessment.",
            )
        candidates.append(TriageCandidateContext(
            candidate_id=candidate.candidate_id, raw_name=candidate.raw_name, commercial_status=linked[0].status,
        ))

    terms = tuple(
        TriageTermContext(term_id=term.term_id, raw_name=term.raw_name, raw_value=term.raw_value)
        for term in result.global_terms
    )

    return TermTriageTask(
        schema_version=schema_version,
        organization_id=result.organization_id,
        document_id=result.document_id,
        preprocessing_run_id=result.preprocessing_run_id,
        analysis_run_id=analysis_run_id,
        document_analysis_schema_version=result.schema_version,
        document_analysis_agent_version=result.agent_version,
        candidates=tuple(candidates),
        terms=terms,
    )


def build_term_triage_result(
    *,
    task: TermTriageTask,
    decisions: tuple[TermTriageDecision, ...],
    schema_version: str,
) -> TermTriageResult:
    """Construct the canonical result only after it exactly covers the task's terms.

    Exactly one decision per task term is required -- not "at least one," not
    "no unknown references": a missing term and an unknown term are both
    construction errors here, the same discipline `build_term_applicability_result`
    applies to term/candidate references.
    """

    known_term_ids = set(task.term_ids)
    decided_term_ids = [decision.term_id for decision in decisions]
    unknown = set(decided_term_ids) - known_term_ids
    if unknown:
        raise TermTriageResultConstructionError(
            "term_triage_unknown_term",
            "A decision references a term outside the loaded analysis batch.",
        )
    missing = known_term_ids - set(decided_term_ids)
    if missing:
        raise TermTriageResultConstructionError(
            "term_triage_incomplete_coverage",
            "The triage result does not cover every term in the loaded analysis batch.",
        )

    return TermTriageResult(
        schema_version=schema_version,
        organization_id=task.organization_id,
        document_id=task.document_id,
        preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id,
        decisions=decisions,
    )
