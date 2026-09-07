"""Define bounded, read-only inputs for the two narrow term-applicability evidence tools.

These schemas receive a task-bound term/candidate identifier plus an evidence
ID already known (by `tools.py`) to belong to it. They carry no session,
storage client, path, or SQL. `organization_id`/`analysis_run_id` exist here
so `PersistedTermApplicabilityTools` can confirm task scope, but a concrete
agent's SDK-facing tool schema (built in `agent.py`) must never expose those
two fields to Claude — they are injected server-side from the fixed task,
exactly as `document_analysis`'s own tool adapters inject
`organization_id`/`preprocessing_run_id`.
"""

import uuid

from maximor.document_analysis.schemas import Identifier
from maximor.term_applicability.schemas import TermApplicabilityModel


class TermEvidenceRegionInput(TermApplicabilityModel):
    """Scope one narrow evidence lookup to a term already present in the fixed task."""

    organization_id: uuid.UUID
    analysis_run_id: uuid.UUID
    term_id: Identifier
    evidence_id: Identifier


class CandidateEvidenceRegionInput(TermApplicabilityModel):
    """Scope one narrow evidence lookup to a candidate already present in the fixed task."""

    organization_id: uuid.UUID
    analysis_run_id: uuid.UUID
    candidate_id: Identifier
    evidence_id: Identifier
