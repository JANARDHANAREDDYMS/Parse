"""Request-bound read-only semantic-review tool service.

The service receives a server-built task and an optional evidence resolver.
It returns only assigned contexts or resolved evidence and never exposes SQL,
filesystem paths, arbitrary identifiers, or persistence operations.
"""

from typing import Any, Protocol

from maximor.normalization.semantic_review.errors import SemanticReviewTaskError
from maximor.normalization.semantic_review.schemas import NormalizationSemanticReviewTask


class SemanticEvidenceResolver(Protocol):
    """Resolve one already-authorized evidence ID through an existing narrow boundary."""

    async def resolve(self, evidence_id: str) -> Any: ...


class NormalizationSemanticReviewTools:
    """Expose only review items and evidence bound to one trusted task."""

    def __init__(self, task: NormalizationSemanticReviewTask, resolver: SemanticEvidenceResolver | None = None, runtime: Any | None = None):
        self._task = task
        self._resolver = resolver
        self._runtime = runtime

    async def get_review_item_context(self, review_item_id: str):
        """Return one assigned item context and record success only after lookup."""
        item = next((item for item in self._task.items if item.review_item_id == review_item_id), None)
        if item is None:
            raise SemanticReviewTaskError("review_item_not_assigned", "The review item is not assigned to this task.")
        self._record("get_review_item_context")
        return item

    async def get_review_item_evidence(self, review_item_id: str, evidence_id: str):
        """Resolve evidence only when both item and evidence ID are authorized."""
        item = await self.get_review_item_context(review_item_id)
        if evidence_id not in item.allowed_evidence_ids:
            raise SemanticReviewTaskError("evidence_not_allowed", "The evidence is not authorized for this review item.")
        if self._resolver is None:
            raise SemanticReviewTaskError("evidence_unavailable", "Review evidence is unavailable.")
        value = await self._resolver.resolve(evidence_id)
        self._record("get_review_item_evidence", evidence_id=evidence_id)
        return value

    async def get_candidate_context(self, candidate_id: str):
        """Return candidate context only if an assigned item names that candidate."""
        items = tuple(item for item in self._task.items if item.candidate_id == candidate_id)
        if not items:
            raise SemanticReviewTaskError("candidate_not_assigned", "The candidate is not assigned to this task.")
        self._record("get_candidate_context")
        return items

    async def get_term_context(self, term_id: str):
        """Return a term reference only when an assigned item names that term."""
        items = tuple(item for item in self._task.items if item.term_id == term_id)
        if not items:
            raise SemanticReviewTaskError("term_not_assigned", "The term is not assigned to this task.")
        self._record("get_term_context")
        return items

    def _record(self, name: str, *, evidence_id: str | None = None) -> None:
        if self._runtime is not None and hasattr(self._runtime, "record_successful_tool"):
            self._runtime.record_successful_tool(name, evidence_id=evidence_id)
