"""Implement the two narrow, task-bound, read-only evidence-region tools.

`PersistedTermApplicabilityTools` is built for one fixed `TermApplicabilityTask`
and serves only that task: it never accepts an organization/analysis-run
identifier that doesn't match it, a term/candidate id outside it, or an
evidence id that did not itself come from that exact term's or candidate's
own evidence during task construction. It exposes no generic, unrestricted
evidence tool, no full-text search, no filesystem, no SQL, and no database
session to its caller — the only thing it can resolve is evidence the task
itself already knows about.
"""

from maximor.document_analysis.tool_schemas import BlockResult, EvidenceRegionInput, TableResult
from maximor.term_applicability.contracts import EvidenceResolver, TermApplicabilityTask
from maximor.term_applicability.errors import (
    CandidateNotFoundError,
    EvidenceIdNotFoundError,
    TaskScopeMismatchError,
    TermNotFoundError,
)
from maximor.term_applicability.tool_schemas import CandidateEvidenceRegionInput, TermEvidenceRegionInput


class PersistedTermApplicabilityTools:
    """Resolve evidence only for the term/candidate/evidence-id combinations a fixed task contains."""

    def __init__(self, resolver: EvidenceResolver, task: TermApplicabilityTask) -> None:
        """Bind to one shared evidence resolver and one fixed, already-validated task."""

        self._resolver = resolver
        self._task = task

    async def get_term_evidence_region(self, request: TermEvidenceRegionInput) -> BlockResult | TableResult:
        """Return one persisted block/table region already cited by one term in this task."""

        self._require_task_scope(request.organization_id, request.analysis_run_id)
        term = next((item for item in self._task.terms if item.term_id == request.term_id), None)
        if term is None:
            raise TermNotFoundError
        if request.evidence_id not in term.evidence_ids:
            raise EvidenceIdNotFoundError
        return await self._resolve(request.evidence_id)

    async def get_candidate_evidence_region(self, request: CandidateEvidenceRegionInput) -> BlockResult | TableResult:
        """Return one persisted block/table region already cited by one candidate in this task."""

        self._require_task_scope(request.organization_id, request.analysis_run_id)
        candidate = next((item for item in self._task.candidates if item.candidate_id == request.candidate_id), None)
        if candidate is None:
            raise CandidateNotFoundError
        if request.evidence_id not in candidate.evidence_ids:
            raise EvidenceIdNotFoundError
        return await self._resolve(request.evidence_id)

    def _require_task_scope(self, organization_id, analysis_run_id) -> None:
        """Reject any request whose organization/analysis-run scope is not this fixed task."""

        if organization_id != self._task.organization_id or analysis_run_id != self._task.analysis_run_id:
            raise TaskScopeMismatchError

    async def _resolve(self, evidence_id: str) -> BlockResult | TableResult:
        """Resolve one already-authorized evidence ID through the shared evidence resolver.

        `evidence_id` membership in the requested term's or candidate's own
        `evidence_ids` was already confirmed by the caller before this runs;
        `self._task.evidence_by_id` is guaranteed to hold every ID that
        appears in any term's or candidate's `evidence_ids`, because both are
        built from the same source evidence in `build_term_applicability_task`.
        """

        evidence = self._task.evidence_by_id[evidence_id]
        return await self._resolver.get_evidence_region(
            EvidenceRegionInput(
                organization_id=self._task.organization_id,
                preprocessing_run_id=self._task.preprocessing_run_id,
                evidence=evidence,
            )
        )
