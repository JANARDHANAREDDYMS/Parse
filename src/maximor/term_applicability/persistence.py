"""Persist combined triage and term-applicability results without invoking agents."""
import gzip, hashlib, uuid
from datetime import UTC, datetime
from collections.abc import AsyncIterator
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from maximor.db.models import TermApplicabilityRun, TermTriageDecisionProjection, TermApplicabilityDecisionProjection, CandidateCommercialFactCoverageProjection, CandidateCommercialFactProjection, DocumentAnalysisRun
from maximor.db.models.term_applicability import TermApplicabilityEvidenceProjection
from maximor.storage import ObjectStorage
from maximor.preprocessing.persistence import canonical_json_bytes, compress_canonical_json, decompress_canonical_json, MAXIMUM_COMPRESSED_RESULT_BYTES, MAXIMUM_UNCOMPRESSED_RESULT_BYTES
from maximor.term_triage.schemas import TermTriageResult
from maximor.term_applicability.errors import TermApplicabilityPersistenceError
from maximor.term_applicability.schemas import TermApplicabilityResult

class CombinedTermApplicabilityArtifact(BaseModel):
    """Canonical snapshot containing both validated enrichment outputs."""
    model_config = ConfigDict(frozen=True)
    triage: TermTriageResult
    applicability: TermApplicabilityResult

async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    """Yield one canonical artifact payload."""
    yield data

class TermApplicabilityPersistenceService:
    """Own combined enrichment run records, compressed artifacts, and projections."""
    def __init__(self, sessions: async_sessionmaker[AsyncSession], storage: ObjectStorage) -> None:
        """Inject database sessions and bounded object storage."""
        self._sessions, self._storage = sessions, storage

    async def create_run(self, *, run_id: uuid.UUID, organization_id: uuid.UUID, document_id: uuid.UUID, analysis_run_id: uuid.UUID, attempt_number: int, schema_version: str, prompt_version: str, skill_version: str, agent_version: str, model: str, started_at: datetime, processing_job_id: uuid.UUID | None = None) -> TermApplicabilityRun:
        """Create or return one running attempt idempotently."""
        async with self._sessions() as session:
            async with session.begin():
                source = await session.get(DocumentAnalysisRun, analysis_run_id)
                if source is None or source.organization_id != organization_id or source.document_id != document_id or source.status != "completed":
                    raise ValueError("term applicability source unavailable")
                existing = await session.get(TermApplicabilityRun, run_id)
                if existing: return existing
                row = TermApplicabilityRun(id=run_id, organization_id=organization_id, document_id=document_id, analysis_run_id=analysis_run_id, processing_job_id=processing_job_id, attempt_number=attempt_number, status="running", schema_version=schema_version, prompt_version=prompt_version, skill_version=skill_version, agent_version=agent_version, model=model, started_at=started_at)
                session.add(row); await session.flush(); return row

    async def save_completed_result(self, *, run_id: uuid.UUID, triage: TermTriageResult, applicability: TermApplicabilityResult, runtime=None) -> None:
        """Store a reproducible gzip artifact and all projections transactionally."""
        artifact = CombinedTermApplicabilityArtifact(triage=triage, applicability=applicability)
        if triage.organization_id != applicability.organization_id or triage.document_id != applicability.document_id or triage.analysis_run_id != applicability.analysis_run_id:
            raise ValueError("term applicability identity mismatch")
        raw = canonical_json_bytes(artifact)
        if len(raw) > MAXIMUM_UNCOMPRESSED_RESULT_BYTES:
            raise ValueError("term applicability artifact too large")
        compressed = compress_canonical_json(raw)
        if len(compressed) > MAXIMUM_COMPRESSED_RESULT_BYTES:
            raise ValueError("term applicability artifact too large")
        key = f"organizations/{triage.organization_id}/documents/{triage.document_id}/term-applicability/{run_id}.json.gz"
        await self._storage.put(key, _chunks(compressed), maximum_bytes=10*1024*1024)
        try:
            async with self._sessions() as session:
                async with session.begin():
                    row = await session.get(TermApplicabilityRun, run_id)
                    if row is None: raise ValueError("term applicability run not found")
                    if row.organization_id != triage.organization_id or row.document_id != triage.document_id or row.analysis_run_id != triage.analysis_run_id:
                        raise ValueError("term applicability identity mismatch")
                    if row.status == "completed" and row.content_sha256_checksum != hashlib.sha256(raw).hexdigest(): raise ValueError("completed result conflict")
                    if row.status == "completed" and row.content_sha256_checksum == hashlib.sha256(raw).hexdigest() and row.canonical_result_storage_key:
                        return
                    now = datetime.now(row.started_at.tzinfo)
                    for i, decision in enumerate(triage.decisions): session.add(TermTriageDecisionProjection(run_id=run_id, organization_id=triage.organization_id, term_id=decision.term_id, disposition=decision.disposition.value, source_order=i, rationale=decision.rationale))
                    for i, decision in enumerate(applicability.decisions): session.add(TermApplicabilityDecisionProjection(run_id=run_id, organization_id=applicability.organization_id, term_id=decision.term_id, disposition=decision.disposition.value, applicability_scope=decision.applicability_scope.value if decision.applicability_scope else None, candidate_ids=list(decision.applies_to_candidate_ids), evidence=[e.model_dump(mode="json") for e in decision.evidence], source_order=i))
                    for coverage in applicability.candidate_commercial_fact_coverage: session.add(CandidateCommercialFactCoverageProjection(run_id=run_id, organization_id=applicability.organization_id, candidate_id=coverage.candidate_id, expected_fields=[x.value for x in coverage.expected_fields], extracted_fields=[x.value for x in coverage.extracted_fields], unresolved_fields=[x.value for x in coverage.unresolved_fields], evidence=[e.model_dump(mode="json") for e in coverage.evidence]))
                    for bundle in applicability.candidate_commercial_facts:
                        for fact in bundle.facts: session.add(CandidateCommercialFactProjection(run_id=run_id, organization_id=applicability.organization_id, candidate_id=bundle.candidate_id, fact_id=fact.fact_id, field=fact.field.value, raw_value=fact.raw_value, raw_period_label=fact.raw_period_label, evidence=[e.model_dump(mode="json") for e in fact.evidence]))
                    evidence_rows = []
                    for owner_type, owner_id, refs in (
                        *[("term_decision", d.term_id, d.evidence) for d in applicability.decisions],
                        *[("commercial_fact", f.fact_id, f.evidence) for b in applicability.candidate_commercial_facts for f in b.facts],
                        *[("fact_coverage", c.candidate_id, c.evidence) for c in applicability.candidate_commercial_fact_coverage],
                    ):
                        for order, ref in enumerate(refs):
                            evidence_rows.append(TermApplicabilityEvidenceProjection(
                                run_id=run_id, organization_id=applicability.organization_id,
                                owner_type=owner_type, owner_id=owner_id,
                                preprocessing_run_id=ref.preprocessing_run_id, page_number=ref.page_number,
                                external_block_id=ref.block_id, external_table_id=ref.table_id,
                                representation=ref.representation.value, extraction_source=ref.extraction_source.value if ref.extraction_source else None,
                                bounding_box=ref.bounding_box.model_dump(mode="json") if ref.bounding_box else None,
                                source_order=order,
                            ))
                    session.add_all(evidence_rows)
                    row.status="completed"; row.completed_at=now; row.canonical_result_storage_key=key; row.compression_method="gzip"; row.compressed_sha256_checksum=hashlib.sha256(compressed).hexdigest(); row.content_sha256_checksum=hashlib.sha256(raw).hexdigest(); row.compressed_size=len(compressed); row.uncompressed_size=len(raw); row.runtime_diagnostics=runtime.persistence_diagnostics() if runtime else None
        except Exception:
            await self._storage.delete(key)
            raise

    async def load_completed_result(self, *, organization_id: uuid.UUID, run_id: uuid.UUID) -> CombinedTermApplicabilityArtifact:
        """Load, checksum-verify, decompress, and validate one completed artifact."""
        async with self._sessions() as session: row = await session.scalar(select(TermApplicabilityRun).where(TermApplicabilityRun.id == run_id, TermApplicabilityRun.organization_id == organization_id, TermApplicabilityRun.status == "completed"))
        if row is None or not row.canonical_result_storage_key: raise ValueError("term applicability result unavailable")
        compressed = await self._storage.read(row.canonical_result_storage_key, maximum_bytes=MAXIMUM_COMPRESSED_RESULT_BYTES)
        if hashlib.sha256(compressed).hexdigest() != row.compressed_sha256_checksum: raise ValueError("term applicability artifact integrity failure")
        try:
            raw = decompress_canonical_json(compressed, maximum_bytes=MAXIMUM_UNCOMPRESSED_RESULT_BYTES)
        except Exception as exc:
            raise ValueError("term applicability artifact is invalid") from exc
        if hashlib.sha256(raw).hexdigest() != row.content_sha256_checksum: raise ValueError("term applicability content integrity failure")
        return CombinedTermApplicabilityArtifact.model_validate_json(raw)

    async def mark_run_failed(
        self, run_id: uuid.UUID, *, error_code: str, error_message: str,
        failure_stage: str | None = None, triage_runtime=None, applicability_runtime=None,
    ) -> None:
        """Mark a non-completed run failed with a safe, stage-tagged diagnostics envelope.

        Unlike the single-agent `document_analysis`/`sku_mapping` handlers,
        one combined enrichment run spans two agents (triage, then
        applicability) -- either, both, or neither may have actually
        executed before the failure. The stored envelope keeps each agent's
        own bounded `persistence_diagnostics()` snapshot (plus token/cache/
        cost fields, copied the same unconditional way
        `document_analysis.persistence._apply_runtime` does) under its own
        key, tagged with which phase (`failure_stage`) the failure itself
        occurred in, so a caller never has to guess which agent a given
        diagnostics blob belongs to.
        """
        diagnostics = None
        if failure_stage is not None or triage_runtime is not None or applicability_runtime is not None:
            diagnostics = {
                "failure_stage": failure_stage,
                "triage": _agent_failure_diagnostics(triage_runtime),
                "applicability": _agent_failure_diagnostics(applicability_runtime),
            }
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(TermApplicabilityRun, run_id)
                if row is None:
                    raise TermApplicabilityPersistenceError("term_applicability_run_not_found")
                if row.status == "completed":
                    raise TermApplicabilityPersistenceError("term_applicability_run_already_completed")
                row.status = "failed"
                row.error_code = error_code[:100]
                row.error_message = error_message[:500]
                row.completed_at = datetime.now(UTC)
                if diagnostics is not None:
                    row.runtime_diagnostics = diagnostics


def _agent_failure_diagnostics(runtime) -> dict | None:
    """Return one agent's bounded safe diagnostics plus token/cache/cost fields, or None."""
    if runtime is None:
        return None
    return {
        "diagnostics": runtime.persistence_diagnostics(),
        "input_tokens": runtime.input_tokens,
        "output_tokens": runtime.output_tokens,
        "cache_creation_input_tokens": runtime.cache_creation_input_tokens,
        "cache_read_input_tokens": runtime.cache_read_input_tokens,
        "cost_usd": runtime.cost_usd,
        "model_usage": runtime.model_usage or None,
    }
