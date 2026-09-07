"""Define the versioned term-applicability input contract and narrow interface boundaries.

`TermApplicabilityTask` is built from one already-validated `DocumentAnalysisResult`
— every product candidate and every global term it contains, batched together for
one completed analysis run. Building a task here makes no applicability decision
about any term; a later, separately configured agent owns that decision. This
module performs no database access, no evidence resolution, and no Claude call.

`CandidateContext`/`TermContext` carry `evidence_ids` (bare block/table identifier
strings), not full `EvidenceReference` objects, in the compact per-term/candidate
summaries: the task is meant to be a compact batch an agent can hold in full. The
task also keeps `evidence_by_id`, a full identifier-to-`EvidenceReference` lookup —
not exposed to Claude directly, never rendered into the compact summaries or the
finalizer schema — so the narrow evidence tools in `tools.py` can resolve an
already-authorized evidence ID to the exact trusted reference `get_evidence_region`
needs, without asking Claude to reconstruct bounding boxes, representations, or
extraction sources itself.
"""

import uuid
from typing import Protocol

from pydantic import Field, model_validator

from maximor.document_analysis.schemas import (
    MAX_EVIDENCE_PER_ENTITY,
    MAX_GLOBAL_TERMS,
    MAX_PRODUCT_CANDIDATES,
    CommercialStatus,
    DocumentAnalysisResult,
    EvidenceReference,
    Identifier,
)
from maximor.document_analysis.tool_schemas import BlockResult, EvidenceRegionInput, TableResult
from maximor.term_applicability.errors import (
    TermApplicabilityResultConstructionError,
    TermApplicabilityTaskConstructionError,
)
from maximor.term_applicability.schemas import (
    CandidateCommercialFacts,
    CandidateCommercialFactCoverage,
    RawCommercialFactField,
    TermApplicabilityDecision,
    TermApplicabilityModel,
    TermApplicabilityResult,
)
from maximor.term_applicability.tool_schemas import CandidateEvidenceRegionInput, TermEvidenceRegionInput
from maximor.term_triage.schemas import TermTriageDisposition, TermTriageResult

# Candidate commercial statuses eligible for downstream raw-fact extraction.
# `excluded`/`optional`/`mentioned`/`ambiguous` candidates never receive
# commercial facts, regardless of what evidence exists for them.
FACT_ELIGIBLE_COMMERCIAL_STATUSES = frozenset({CommercialStatus.PURCHASED, CommercialStatus.INCLUDED})
RAW_ATTRIBUTE_FACT_HINTS = {
    "qty": RawCommercialFactField.QUANTITY,
    "unit_price": RawCommercialFactField.UNIT_PRICE,
    "total_contract_value": RawCommercialFactField.TOTAL_LISTED_VALUE,
}


def _evidence_ids(evidence) -> tuple[str, ...]:
    """Return deterministic, deduplicated block/table identifiers for compact evidence."""

    seen: list[str] = []
    for reference in evidence:
        identifier = reference.block_id or reference.table_id
        if identifier not in seen:
            seen.append(identifier)
    return tuple(sorted(seen))


def _index_evidence_by_id(*evidence_groups: tuple) -> dict[str, EvidenceReference]:
    """Return one identifier-to-reference lookup, merging every group's evidence.

    An identifier appearing in more than one group (a term and a candidate
    citing the same underlying block or table) always carries the same
    content, since both point at the same persisted row — merging is safe.
    """

    index: dict[str, EvidenceReference] = {}
    for group in evidence_groups:
        for reference in group:
            identifier = reference.block_id or reference.table_id
            index[identifier] = reference
    return index


class CandidateContext(TermApplicabilityModel):
    """Represent one product candidate's compact facts for term-applicability reasoning.

    No SKU mapping decision and no normalized values appear here — only the
    same raw facts `DocumentAnalysisAgent` already extracted.
    """

    candidate_id: Identifier
    raw_name: str = Field(min_length=1, max_length=2_000)
    commercial_status: CommercialStatus
    raw_attributes: dict[str, str | None] = Field(default_factory=dict)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)

    @property
    def expected_fact_fields(self) -> tuple[RawCommercialFactField, ...]:
        """Return raw commercial fields whose source hints require investigation."""

        if self.commercial_status not in FACT_ELIGIBLE_COMMERCIAL_STATUSES:
            return ()
        return tuple(sorted({field for key, field in RAW_ATTRIBUTE_FACT_HINTS.items() if self.raw_attributes.get(key)}, key=lambda item: item.value))


class TermContext(TermApplicabilityModel):
    """Represent one raw global term's compact facts for term-applicability reasoning."""

    term_id: Identifier
    raw_name: str = Field(min_length=1, max_length=500)
    raw_value: str | None = Field(default=None, max_length=4_000)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=MAX_EVIDENCE_PER_ENTITY)


class TermApplicabilityTask(TermApplicabilityModel):
    """Carry every candidate and every term for one completed analysis run.

    This is a batch: one task covers an entire `document_analysis_run`, not
    one term or one candidate — a future agent resolves the whole run's terms
    in one invocation rather than one Claude call per term.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    document_analysis_schema_version: str = Field(min_length=1, max_length=50)
    document_analysis_agent_version: str = Field(min_length=1, max_length=100)
    candidates: tuple[CandidateContext, ...] = Field(default=(), max_length=MAX_PRODUCT_CANDIDATES)
    terms: tuple[TermContext, ...] = Field(default=(), max_length=MAX_GLOBAL_TERMS)
    evidence_by_id: dict[Identifier, EvidenceReference] = Field(default_factory=dict)
    selected_term_ids: tuple[Identifier, ...] | None = None

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        """Return every loaded candidate's identifier, for membership checks."""

        return tuple(item.candidate_id for item in self.candidates)

    @property
    def candidates_by_id(self) -> dict[str, CandidateContext]:
        """Return every loaded candidate keyed by ID, for eligibility/status lookups."""

        return {item.candidate_id: item for item in self.candidates}

    @property
    def term_ids(self) -> tuple[str, ...]:
        """Return every loaded term's identifier -- the full audit set, not the selection."""

        return tuple(item.term_id for item in self.terms)

    @property
    def effective_selected_term_ids(self) -> tuple[str, ...]:
        """Return the trusted subset eligible for attribution.

        `None` means "no triage has run yet" and defaults to every term
        being eligible -- the same behavior every `TermApplicabilityTask`
        had before triage existed, so untriaged callers and every existing
        test remain valid without change. An explicit tuple (including an
        empty one, if triage found nothing worth attributing) always means
        exactly that set, never "everything."
        """

        return self.term_ids if self.selected_term_ids is None else self.selected_term_ids

    @property
    def expected_fact_fields_by_candidate(self) -> dict[str, tuple[RawCommercialFactField, ...]]:
        """Return deterministic expected-field hints for eligible candidates."""

        return {candidate.candidate_id: candidate.expected_fact_fields for candidate in self.candidates if candidate.expected_fact_fields}

    @model_validator(mode="after")
    def identifiers_are_unique_and_ordered(self) -> "TermApplicabilityTask":
        """Require deterministic ascending IDs with no duplicates in either collection."""

        candidate_ids = list(self.candidate_ids)
        if candidate_ids != sorted(candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("task candidates must be unique and ordered by candidate_id")
        term_ids = list(self.term_ids)
        if term_ids != sorted(term_ids) or len(term_ids) != len(set(term_ids)):
            raise ValueError("task terms must be unique and ordered by term_id")
        if self.selected_term_ids is not None:
            selected = list(self.selected_term_ids)
            if selected != sorted(selected) or len(selected) != len(set(selected)):
                raise ValueError("selected_term_ids must be unique and ordered")
            if any(term_id not in set(term_ids) for term_id in selected):
                raise ValueError("selected_term_ids must belong to this task's own terms")
        return self


def build_term_applicability_task(
    *,
    result: DocumentAnalysisResult,
    analysis_run_id: uuid.UUID,
    schema_version: str,
) -> TermApplicabilityTask:
    """Build one batch task from every candidate and term in an already-validated result.

    Requires exactly one commercial-status assessment linked to each product
    candidate, mirroring `build_sku_mapping_task`'s check: a candidate with
    zero or more than one linked assessment is an unresolved upstream
    modelling ambiguity, not something this batch construction can silently
    paper over. Both `result.product_candidates` and `result.global_terms`
    are already unique and ordered by ID (enforced by `DocumentAnalysisResult`
    itself), so no re-sorting happens here.
    """

    candidates: list[CandidateContext] = []
    all_evidence_groups: list[tuple] = []
    for candidate in result.product_candidates:
        linked = [item for item in result.commercial_statuses if item.candidate_id == candidate.candidate_id]
        if len(linked) == 0:
            raise TermApplicabilityTaskConstructionError(
                "term_applicability_status_missing",
                "A product candidate has no linked commercial-status assessment.",
            )
        if len(linked) > 1:
            raise TermApplicabilityTaskConstructionError(
                "term_applicability_status_ambiguous",
                "A product candidate has more than one linked commercial-status assessment.",
            )
        status = linked[0]
        candidates.append(CandidateContext(
            candidate_id=candidate.candidate_id,
            raw_name=candidate.raw_name,
            commercial_status=status.status,
            raw_attributes=dict(candidate.raw_attributes),
            evidence_ids=_evidence_ids(candidate.evidence),
        ))
        all_evidence_groups.append(candidate.evidence)

    terms = tuple(
        TermContext(
            term_id=term.term_id,
            raw_name=term.raw_name,
            raw_value=term.raw_value,
            evidence_ids=_evidence_ids(term.evidence),
        )
        for term in result.global_terms
    )
    all_evidence_groups.extend(term.evidence for term in result.global_terms)

    return TermApplicabilityTask(
        schema_version=schema_version,
        organization_id=result.organization_id,
        document_id=result.document_id,
        preprocessing_run_id=result.preprocessing_run_id,
        analysis_run_id=analysis_run_id,
        document_analysis_schema_version=result.schema_version,
        document_analysis_agent_version=result.agent_version,
        candidates=tuple(candidates),
        terms=terms,
        evidence_by_id=_index_evidence_by_id(*all_evidence_groups),
    )


def build_term_applicability_result(
    *,
    task: TermApplicabilityTask,
    decisions: tuple[TermApplicabilityDecision, ...],
    schema_version: str,
    candidate_commercial_facts: tuple[CandidateCommercialFacts, ...] = (),
    candidate_commercial_fact_coverage: tuple[CandidateCommercialFactCoverage, ...] = (),
) -> TermApplicabilityResult:
    """Construct the canonical result only after every decision/fact resolves within the task.

    A decision naming a `term_id` outside `task.effective_selected_term_ids`
    is rejected here even when that term genuinely exists in the task's full
    audit list (`task.term_ids`) -- triage explicitly did not select it for
    attribution, and this agent may not decide about it anyway. A decision
    naming an `applies_to_candidate_ids` entry absent from the task's own
    candidates is rejected the same way `DocumentAnalysisResult` rejects a
    global term naming an unknown product candidate.

    A `CandidateCommercialFacts` bundle naming a candidate absent from the
    task, or naming a real candidate whose commercial status is not
    `purchased`/`included`, is rejected the same way -- facts are never
    produced for `excluded`/`optional`/`mentioned`/`ambiguous` candidates,
    regardless of what evidence exists for them.
    """

    known_term_ids = set(task.term_ids)
    selected_term_ids = set(task.effective_selected_term_ids)
    known_candidate_ids = set(task.candidate_ids)
    for decision in decisions:
        if decision.term_id not in known_term_ids:
            raise TermApplicabilityResultConstructionError(
                "term_applicability_unknown_term",
                "A decision references a term outside the loaded analysis batch.",
            )
        if decision.term_id not in selected_term_ids:
            raise TermApplicabilityResultConstructionError(
                "term_applicability_term_not_selected",
                "A decision references a term not selected for attribution.",
            )
        for candidate_id in decision.applies_to_candidate_ids:
            if candidate_id not in known_candidate_ids:
                raise TermApplicabilityResultConstructionError(
                    "term_applicability_unknown_candidate",
                    "A decision references a candidate outside the loaded analysis batch.",
                )

    candidates_by_id = task.candidates_by_id
    for bundle in candidate_commercial_facts:
        candidate = candidates_by_id.get(bundle.candidate_id)
        if candidate is None:
            raise TermApplicabilityResultConstructionError(
                "term_applicability_fact_unknown_candidate",
                "A commercial-fact bundle references a candidate outside the loaded analysis batch.",
            )
        if candidate.commercial_status not in FACT_ELIGIBLE_COMMERCIAL_STATUSES:
            raise TermApplicabilityResultConstructionError(
                "term_applicability_fact_candidate_not_eligible",
                "A commercial-fact bundle references a candidate not eligible for fact extraction.",
            )

    expected_by_candidate = task.expected_fact_fields_by_candidate
    for coverage in candidate_commercial_fact_coverage:
        if coverage.candidate_id not in expected_by_candidate:
            raise TermApplicabilityResultConstructionError(
                "term_applicability_coverage_candidate_not_eligible",
                "A coverage declaration references a candidate without expected raw-fact hints.",
            )
        if tuple(coverage.expected_fields) != tuple(expected_by_candidate[coverage.candidate_id]):
            raise TermApplicabilityResultConstructionError(
                "term_applicability_coverage_expected_fields_mismatch",
                "Coverage expected fields do not match application-derived hints.",
            )

    return TermApplicabilityResult(
        schema_version=schema_version,
        organization_id=task.organization_id,
        document_id=task.document_id,
        preprocessing_run_id=task.preprocessing_run_id,
        analysis_run_id=task.analysis_run_id,
        decisions=decisions,
        candidate_commercial_facts=candidate_commercial_facts,
        candidate_commercial_fact_coverage=candidate_commercial_fact_coverage,
    )


def build_selected_term_applicability_task(
    full_task: TermApplicabilityTask,
    triage_result: TermTriageResult,
) -> TermApplicabilityTask:
    """Derive the attribution-eligible task from a completed triage pass.

    Selected terms are every `potential_line_item` and every `uncertain`
    triage decision; `document_metadata` terms are not automatically
    selected. Requires the triage result to cover exactly `full_task`'s own
    terms and to share its identity (organization/document/preprocessing/
    analysis-run) -- a triage batch for a different task cannot silently
    narrow this one. The returned task is a new, fully re-validated
    `TermApplicabilityTask`, identical to `full_task` except for
    `selected_term_ids`; `full_task.terms` (the complete audit set) is
    unchanged, so an omitted `document_metadata` term is preserved as
    un-attributed, never as an implicit `unknown` decision.
    """

    if (
        triage_result.organization_id, triage_result.document_id,
        triage_result.preprocessing_run_id, triage_result.analysis_run_id,
    ) != (
        full_task.organization_id, full_task.document_id,
        full_task.preprocessing_run_id, full_task.analysis_run_id,
    ):
        raise TermApplicabilityTaskConstructionError(
            "term_applicability_triage_identity_mismatch",
            "The triage result does not match this task's identity.",
        )

    triaged_term_ids = {decision.term_id for decision in triage_result.decisions}
    if triaged_term_ids != set(full_task.term_ids):
        raise TermApplicabilityTaskConstructionError(
            "term_applicability_triage_coverage_mismatch",
            "The triage result does not exactly cover this task's own terms.",
        )

    selectable = {TermTriageDisposition.POTENTIAL_LINE_ITEM, TermTriageDisposition.UNCERTAIN}
    selected = sorted(
        decision.term_id for decision in triage_result.decisions
        if decision.disposition in selectable
    )

    return TermApplicabilityTask(
        schema_version=full_task.schema_version,
        organization_id=full_task.organization_id,
        document_id=full_task.document_id,
        preprocessing_run_id=full_task.preprocessing_run_id,
        analysis_run_id=full_task.analysis_run_id,
        document_analysis_schema_version=full_task.document_analysis_schema_version,
        document_analysis_agent_version=full_task.document_analysis_agent_version,
        candidates=full_task.candidates,
        terms=full_task.terms,
        evidence_by_id=full_task.evidence_by_id,
        selected_term_ids=tuple(selected),
    )


class EvidenceResolver(Protocol):
    """Expose only evidence-region resolution, reusing `document_analysis`'s own contract.

    `EvidenceRegionInput`/`BlockResult`/`TableResult` are imported directly
    from `document_analysis.tool_schemas` rather than redefined, exactly as
    `sku_mapping.contracts.EvidenceResolver` already does: evidence resolution
    reads from the same `document_blocks`/`document_tables` tables for the
    same `preprocessing_run_id` regardless of which agent is asking.
    """

    async def get_evidence_region(self, request: EvidenceRegionInput) -> BlockResult | TableResult: ...


class TermApplicabilityToolset(Protocol):
    """Expose exactly the three narrow, tenant-scoped read-only tools this agent needs.

    `get_term_evidence_region`/`get_candidate_evidence_region` are scoped to
    one already-loaded `TermApplicabilityTask` (organization + analysis run +
    a term/candidate id already present in that task) — a concrete
    implementation is not a general document-search tool and cannot be
    redirected toward evidence outside the fixed task. There is no full-text
    `search_document` here: this agent starts from the compact task context
    and only ever resolves evidence it already knows the ID of.
    """

    async def get_term_evidence_region(self, request: TermEvidenceRegionInput) -> BlockResult | TableResult: ...

    async def get_candidate_evidence_region(self, request: CandidateEvidenceRegionInput) -> BlockResult | TableResult: ...

    async def get_evidence_region(self, request: EvidenceRegionInput) -> BlockResult | TableResult: ...
