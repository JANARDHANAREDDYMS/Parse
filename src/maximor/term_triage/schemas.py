"""Define versioned term-triage decision and result output contracts.

These schemas receive a proposed disposition per term and return validated
structured output. They perform no database access and no Claude call —
`TermTriageDecision` is the shape a future `ClaudeTermTriageAgent` must
produce; nothing here decides anything.

Nothing here can express a SKU mapping, money/date/quantity normalization, a
commercial-status change, an evidence citation, or an applicability claim:
those fields simply do not exist on `TermTriageDecision`. This is the
enforcement mechanism, not a runtime check — triage is a coarse
classification pass, not an attribution decision.
"""

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maximor.document_analysis.schemas import MAX_GLOBAL_TERMS, Identifier


class TermTriageModel(BaseModel):
    """Forbid undocumented fields and keep term-triage contracts immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TermTriageDisposition(StrEnum):
    """Classify one raw term's rough shape before any evidence-grounded attribution.

    `DOCUMENT_METADATA` means "not automatically sent to applicability
    attribution" -- not discarded, and not impossible to revisit later.
    `POTENTIAL_LINE_ITEM` and `UNCERTAIN` are both sent to
    `TermApplicabilityAgent`; `UNCERTAIN` exists specifically so triage never
    has to force a confident metadata classification just to produce an
    answer -- when in doubt, this is the safe choice, not `DOCUMENT_METADATA`.
    """

    DOCUMENT_METADATA = "document_metadata"
    POTENTIAL_LINE_ITEM = "potential_line_item"
    UNCERTAIN = "uncertain"


class TermTriageDecision(TermTriageModel):
    """Return one coarse disposition for one term, with a concise, bounded rationale.

    Deliberately has no `applicability_scope`, no candidate IDs, and no
    evidence field of any kind -- triage never reasons about which candidate
    a term applies to, only whether the term is a commercially relevant
    line-item candidate at all.
    """

    term_id: Identifier
    disposition: TermTriageDisposition
    rationale: str | None = Field(default=None, max_length=300)


class TermTriageResult(TermTriageModel):
    """Return one versioned batch of triage decisions for one analysis run.

    Coverage completeness (exactly one decision per task term) is enforced
    by `build_term_triage_result`/`validate_term_triage_result` against the
    originating `TermTriageTask`, not by a field on this type -- mirroring
    how `TermApplicabilityResult` enforces task membership externally rather
    than duplicating the task's own term list inside every contract that
    needs to reference it.
    """

    schema_version: str = Field(min_length=1, max_length=50)
    organization_id: uuid.UUID
    document_id: uuid.UUID
    preprocessing_run_id: uuid.UUID
    analysis_run_id: uuid.UUID
    decisions: tuple[TermTriageDecision, ...] = Field(default=(), max_length=MAX_GLOBAL_TERMS)

    @model_validator(mode="after")
    def decisions_are_unique_and_ordered(self) -> "TermTriageResult":
        """Require deterministic ascending term IDs with no duplicates."""

        term_ids = [decision.term_id for decision in self.decisions]
        if term_ids != sorted(term_ids) or len(term_ids) != len(set(term_ids)):
            raise ValueError("term-triage decisions must be unique and ordered by term_id")
        return self
