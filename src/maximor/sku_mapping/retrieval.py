"""Implement the demo-scale deterministic hybrid SKU retriever.

`DeterministicHybridSkuRetriever` is the take-home-scale lexical search path:
it fetches every active SKU for a tenant's active catalog version in one call
and scores all of them in pure Python. That is appropriate for the ~21-SKU
demo catalog and keeps scoring fully deterministic and unit-testable without a
database connection, but it is **not** the intended production search path.

A production `HybridSkuRetriever` implementation should push lexical
similarity into PostgreSQL (`CREATE EXTENSION pg_trgm`, `similarity()` /
`word_similarity()` over a GIN trigram index on `skus.name`/`sku_code`) rather
than scoring every row client-side, and should add a real `SemanticSkuSource`
backed by pgvector embeddings. The semantic seam on `SkuRepository` /
`HybridSkuRetriever` in `contracts.py` is wired up and merged into ranking
below, but no implementation of it exists yet: no embeddings are computed and
no external call is made by this module, because nothing in this codebase
constructs a `SemanticSkuSource` to pass in.
"""

import re
import uuid
from collections import Counter

from maximor.sku_mapping.contracts import SemanticSkuSource, SkuMappingTask, SkuRepository
from maximor.sku_mapping.errors import ActiveCatalogVersionNotFoundError, InvalidRetrievalLimitError
from maximor.sku_mapping.schemas import MAX_RETRIEVED_SKUS, RetrievedSku, SkuMatchSource, SkuRecord, SkuRetrievalResult
from maximor.sku_mapping.versions import HYBRID_SKU_RETRIEVER_VERSION, SKU_RETRIEVAL_SCHEMA_VERSION

# Below this Dice-coefficient trigram score, a candidate is treated as noise
# and is not surfaced as a lexical match. Tuned by inspection for the demo
# catalog, not calibrated against a labelled dataset.
DEMO_LEXICAL_TRIGRAM_MIN_SCORE = 0.15

# Raw-attribute keys the document-analysis agent has been observed to emit
# that carry additional descriptive text worth matching, beyond `raw_name`.
_DESCRIPTIVE_ATTRIBUTE_KEYS = ("sku_description", "product_description", "description", "product_name")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(value: str) -> str:
    """Lowercase, fold separators to spaces, drop punctuation, collapse whitespace."""

    lowered = value.lower().replace("_", " ").replace("-", " ")
    cleaned = _NON_ALNUM_RE.sub(" ", lowered)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _character_trigrams(value: str) -> Counter:
    """Return padded character 3-gram counts, falling back for short strings."""

    padded = f" {value} "
    if len(padded) < 3:
        return Counter({padded: 1})
    return Counter(padded[index : index + 3] for index in range(len(padded) - 2))


def _trigram_similarity(left: str, right: str) -> float:
    """Return the Dice coefficient over character trigrams, bounded to [0, 1]."""

    if not left or not right:
        return 0.0
    left_grams, right_grams = _character_trigrams(left), _character_trigrams(right)
    total = sum(left_grams.values()) + sum(right_grams.values())
    if total == 0:
        return 0.0
    intersection = sum((left_grams & right_grams).values())
    return (2.0 * intersection) / total


def _query_texts(task: SkuMappingTask) -> tuple[str, ...]:
    """Return deduplicated candidate text to match, in a fixed deterministic order."""

    texts = [task.raw_name]
    for key in _DESCRIPTIVE_ATTRIBUTE_KEYS:
        value = task.raw_attributes.get(key)
        if value:
            texts.append(value)
    deduplicated: list[str] = []
    for text in texts:
        if text not in deduplicated:
            deduplicated.append(text)
    return tuple(deduplicated)


def _score_sku(sku: SkuRecord, query_texts: tuple[str, ...]) -> RetrievedSku | None:
    """Score one catalog row against all query texts using exact/alias then trigram."""

    best_score = 0.0
    matched_text: str | None = None
    sources: set[SkuMatchSource] = set()

    exact_alias_candidates: list[tuple[SkuMatchSource, str]] = [
        (SkuMatchSource.EXACT, sku.name),
        (SkuMatchSource.EXACT, sku.sku_code.replace("_", " ")),
        *((SkuMatchSource.ALIAS, alias) for alias in sku.aliases),
    ]
    lexical_candidates: list[str] = [sku.name, sku.sku_code.replace("_", " "), *sku.aliases]
    if sku.description:
        lexical_candidates.append(sku.description)

    for query_text in query_texts:
        normalized_query = _normalize(query_text)
        if not normalized_query:
            continue
        for source, catalog_text in exact_alias_candidates:
            if normalized_query == _normalize(catalog_text):
                sources.add(source)
                if best_score < 1.0:
                    best_score = 1.0
                    matched_text = catalog_text
        for catalog_text in lexical_candidates:
            normalized_catalog = _normalize(catalog_text)
            if normalized_query == normalized_catalog:
                continue  # already captured as an exact/alias match above.
            score = _trigram_similarity(normalized_query, normalized_catalog)
            if score >= DEMO_LEXICAL_TRIGRAM_MIN_SCORE:
                sources.add(SkuMatchSource.LEXICAL)
                if score > best_score:
                    best_score = score
                    matched_text = catalog_text

    if not sources:
        return None
    return RetrievedSku(
        sku=sku,
        score=round(best_score, 6),
        matched_sources=tuple(sorted(sources, key=lambda source: source.value)),
        matched_text=matched_text,
    )


def _merge_into(merged: dict[uuid.UUID, RetrievedSku], hit: RetrievedSku) -> None:
    """Fold one additional-source hit into the per-SKU merged ranking in place.

    Matches the same rule `_score_sku` already uses internally across query
    texts: keep the higher score, union the contributing sources, and keep
    the matched text of whichever score currently wins (ties favor the
    existing entry, so merge order does not change the outcome for equal
    scores).
    """

    existing = merged.get(hit.sku.id)
    if existing is None:
        merged[hit.sku.id] = hit
        return
    best_score = max(existing.score, hit.score)
    combined_sources = tuple(
        sorted({*existing.matched_sources, *hit.matched_sources}, key=lambda source: source.value)
    )
    matched_text = existing.matched_text if existing.score >= hit.score else hit.matched_text
    merged[hit.sku.id] = RetrievedSku(
        sku=existing.sku, score=round(best_score, 6), matched_sources=combined_sources, matched_text=matched_text
    )


class DeterministicHybridSkuRetriever:
    """Implement `HybridSkuRetriever` with demo-scale in-process exact/alias/trigram scoring.

    `semantic_source` is accepted but never invoked: pgvector-backed semantic
    retrieval is reserved for a later production implementation, not this one.
    """

    def __init__(self, repository: SkuRepository, semantic_source: SemanticSkuSource | None = None) -> None:
        """Receive the read-only catalog repository and an unused reserved semantic seam."""

        self._repository = repository
        self._semantic_source = semantic_source

    async def retrieve(self, task: SkuMappingTask, *, limit: int = 10) -> SkuRetrievalResult:
        """Fetch the tenant's active catalog and return a stable ranked retrieval.

        Exact/alias/lexical scoring always runs. When a `semantic_source` was
        supplied, its hits are merged into the same per-SKU ranking (higher
        score wins, contributing sources are unioned) rather than kept as a
        separate list — so a future real semantic source changes what gets
        found, not how results are assembled or reported.
        """

        if not 1 <= limit <= MAX_RETRIEVED_SKUS:
            raise InvalidRetrievalLimitError
        catalog_version = await self._repository.get_active_catalog_version(task.organization_id)
        if catalog_version is None:
            raise ActiveCatalogVersionNotFoundError
        skus = await self._repository.list_active_skus(task.organization_id, catalog_version.id)
        query_texts = _query_texts(task)

        merged: dict[uuid.UUID, RetrievedSku] = {}
        for sku in skus:
            scored = _score_sku(sku, query_texts)
            if scored is not None:
                merged[sku.id] = scored

        if self._semantic_source is not None:
            for hit in await self._semantic_source.search(task, catalog_version, limit=limit):
                _merge_into(merged, hit)

        ranked = sorted(merged.values(), key=lambda item: (-item.score, item.sku.sku_code))
        return SkuRetrievalResult(
            schema_version=SKU_RETRIEVAL_SCHEMA_VERSION,
            retriever_version=HYBRID_SKU_RETRIEVER_VERSION,
            organization_id=task.organization_id,
            catalog_version_id=catalog_version.id,
            catalog_version_identifier=catalog_version.version_identifier,
            candidate_id=task.candidate_id,
            candidates=tuple(ranked[:limit]),
        )
