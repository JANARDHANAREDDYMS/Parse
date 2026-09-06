"""Define bounded, operation-only inputs for the three SKU-mapping-agent tools.

These schemas carry no sessions, storage clients, or SQL. Trusted scope
(organization, candidate, catalog version) is never a caller-supplied field
here — it is injected server-side by the tool adapter, the same convention
`document_analysis/tool_schemas.py` uses for its own tools.
"""

import uuid

from pydantic import Field, model_validator

from maximor.sku_mapping.schemas import MAX_RETRIEVED_SKUS, SkuMappingModel

DEFAULT_RETRIEVAL_LIMIT = 10


class RetrieveSkusInput(SkuMappingModel):
    """Request a ranked SKU shortlist for the agent's own fixed, trusted candidate.

    Deliberately carries no free-text query. Unlike `document_analysis`'s
    `search_document` — which explores an unknown document, so a query makes
    sense — the SKU-mapping agent is always scoped to one already-built
    `SkuMappingTask`. Letting the caller supply arbitrary search text here
    would let it redirect retrieval away from the candidate's own
    evidence-grounded `raw_name`/`raw_attributes`. Only how many results to
    return is configurable.
    """

    limit: int = Field(default=DEFAULT_RETRIEVAL_LIMIT, ge=1, le=MAX_RETRIEVED_SKUS)


class GetAuthoritativeSkuInput(SkuMappingModel):
    """Independently re-confirm exactly one catalog SKU by id or by code.

    Requires exactly one of `sku_id`/`sku_code` — whichever the agent already
    holds from a retrieval result — so a proposed MATCH can be checked against
    the authoritative catalog record rather than trusted from the retrieval
    shortlist's copy of that data alone.
    """

    sku_id: uuid.UUID | None = None
    sku_code: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def exactly_one_lookup_key(self) -> "GetAuthoritativeSkuInput":
        """Reject a call naming both or neither of sku_id/sku_code."""

        if (self.sku_id is None) == (self.sku_code is None):
            raise ValueError("exactly one of sku_id or sku_code is required")
        return self
