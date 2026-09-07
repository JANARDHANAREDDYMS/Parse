"""Load one trusted `NormalizationInput` from already-completed persisted artifacts.

This never reopens the PDF, never accesses raw preprocessing artifacts, and
exposes no SQL or session to callers outside this module. It reuses
`DocumentAnalysisPersistenceService.load_completed_result` and
`TermApplicabilityPersistenceService.load_completed_result` for their
respective trusted content, and resolves each eligible candidate's current
(non-superseded), completed `SkuMappingRunArtifact` through
`SkuMappingPersistenceService.load_completed_result` after finding that run's
id with one narrow, tenant-scoped query -- the only raw SQL in this package.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maximor.db.models import DocumentAnalysisRun, DocumentProductCandidate, SkuMappingRun, TermApplicabilityRun
from maximor.document_analysis.errors import DocumentAnalysisError
from maximor.document_analysis.persistence import DocumentAnalysisPersistenceService
from maximor.normalization.assembly import assemble_normalization_input
from maximor.normalization.contracts import NormalizationInput, eligible_candidate_ids_of
from maximor.normalization.errors import (
    NormalizationAnalysisRunNotCompletedError,
    NormalizationAnalysisRunNotFoundError,
    NormalizationSkuMappingMissingError,
    NormalizationSourceMismatchError,
    NormalizationTermApplicabilityRunNotCompletedError,
    NormalizationTermApplicabilityRunNotFoundError,
)
from maximor.normalization.versions import NORMALIZATION_INPUT_SCHEMA_VERSION
from maximor.sku_mapping.contracts import SkuMappingRunArtifact
from maximor.sku_mapping.errors import SkuMappingError
from maximor.sku_mapping.persistence import SkuMappingPersistenceService
from maximor.term_applicability.persistence import TermApplicabilityPersistenceService


class NormalizationRepository:
    """Load one trusted `NormalizationInput` for one completed analysis/enrichment run pair."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analysis_results: DocumentAnalysisPersistenceService,
        term_applicability_results: TermApplicabilityPersistenceService,
        sku_mapping_results: SkuMappingPersistenceService,
    ) -> None:
        """Receive the application session factory and the three existing persistence services."""

        self._sessions = sessions
        self._analysis_results = analysis_results
        self._term_applicability_results = term_applicability_results
        self._sku_mapping_results = sku_mapping_results

    async def load_normalization_input(
        self,
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        term_applicability_run_id: uuid.UUID,
        schema_version: str = NORMALIZATION_INPUT_SCHEMA_VERSION,
    ) -> NormalizationInput:
        """Confirm tenant/run identity, load every completed source, and assemble one input.

        Confirms, in order: the analysis run and the term-applicability run
        both exist for this organization/document (and, for the
        term-applicability run, that it names this exact analysis run) and
        have each reached `completed` status; the canonical document-analysis
        and combined term-applicability artifacts (already checksum-verified
        and Pydantic-revalidated by their own persistence services) agree
        with the requested identity; and every eligible candidate resolves to
        a current, completed SKU-mapping run. Only then is a
        `NormalizationInput` assembled from them.
        """

        async with self._sessions() as session:
            analysis_run = await session.scalar(
                select(DocumentAnalysisRun).where(
                    DocumentAnalysisRun.id == analysis_run_id,
                    DocumentAnalysisRun.organization_id == organization_id,
                    DocumentAnalysisRun.document_id == document_id,
                )
            )
        if analysis_run is None:
            raise NormalizationAnalysisRunNotFoundError
        if analysis_run.status != "completed":
            raise NormalizationAnalysisRunNotCompletedError

        async with self._sessions() as session:
            enrichment_run = await session.scalar(
                select(TermApplicabilityRun).where(
                    TermApplicabilityRun.id == term_applicability_run_id,
                    TermApplicabilityRun.organization_id == organization_id,
                    TermApplicabilityRun.document_id == document_id,
                    TermApplicabilityRun.analysis_run_id == analysis_run_id,
                )
            )
        if enrichment_run is None:
            raise NormalizationTermApplicabilityRunNotFoundError
        if enrichment_run.status != "completed":
            raise NormalizationTermApplicabilityRunNotCompletedError

        try:
            document_analysis = await self._analysis_results.load_completed_result(
                organization_id=organization_id, analysis_run_id=analysis_run_id,
            )
        except DocumentAnalysisError:
            raise NormalizationAnalysisRunNotFoundError from None
        if document_analysis.document_id != document_id:
            raise NormalizationSourceMismatchError

        try:
            combined = await self._term_applicability_results.load_completed_result(
                organization_id=organization_id, run_id=term_applicability_run_id,
            )
        except ValueError:
            raise NormalizationTermApplicabilityRunNotFoundError from None

        term_triage, term_applicability = combined.triage, combined.applicability
        if (
            term_applicability.document_id != document_id
            or term_applicability.analysis_run_id != analysis_run_id
            or term_applicability.preprocessing_run_id != document_analysis.preprocessing_run_id
        ):
            raise NormalizationSourceMismatchError

        sku_mappings: dict[str, SkuMappingRunArtifact] = {}
        sku_mapping_run_ids: dict[str, uuid.UUID] = {}
        for candidate_id in eligible_candidate_ids_of(document_analysis):
            run_id = await self._resolve_current_sku_mapping_run_id(
                organization_id=organization_id, analysis_run_id=analysis_run_id, candidate_id=candidate_id,
            )
            if run_id is None:
                raise NormalizationSkuMappingMissingError
            try:
                artifact = await self._sku_mapping_results.load_completed_result(
                    organization_id=organization_id, run_id=run_id,
                )
            except SkuMappingError:
                raise NormalizationSkuMappingMissingError from None
            sku_mappings[candidate_id] = artifact
            sku_mapping_run_ids[candidate_id] = run_id

        return assemble_normalization_input(
            organization_id=organization_id,
            document_id=document_id,
            analysis_run_id=analysis_run_id,
            term_applicability_run_id=term_applicability_run_id,
            document_analysis=document_analysis,
            term_triage=term_triage,
            term_applicability=term_applicability,
            sku_mappings=sku_mappings,
            sku_mapping_run_ids=sku_mapping_run_ids,
            schema_version=schema_version,
        )

    async def _resolve_current_sku_mapping_run_id(
        self, *, organization_id: uuid.UUID, analysis_run_id: uuid.UUID, candidate_id: str,
    ) -> uuid.UUID | None:
        """Return the one current (non-superseded), completed run id for one candidate, or None.

        "Current" mirrors the derived-supersession rule already settled for
        `sku_mapping_runs`: a completed run is current unless some other
        completed run's `supersedes_mapping_run_id` names it. Ties (more than
        one current completed run, which should not happen in practice) are
        broken by the highest `attempt_number`.
        """

        async with self._sessions() as session:
            candidate = await session.scalar(
                select(DocumentProductCandidate).where(
                    DocumentProductCandidate.organization_id == organization_id,
                    DocumentProductCandidate.analysis_run_id == analysis_run_id,
                    DocumentProductCandidate.external_candidate_id == candidate_id,
                )
            )
            if candidate is None:
                return None
            runs = (
                await session.scalars(
                    select(SkuMappingRun).where(
                        SkuMappingRun.organization_id == organization_id,
                        SkuMappingRun.document_product_candidate_id == candidate.id,
                        SkuMappingRun.status == "completed",
                    )
                )
            ).all()
        if not runs:
            return None
        superseded_ids = {run.supersedes_mapping_run_id for run in runs if run.supersedes_mapping_run_id is not None}
        current = [run for run in runs if run.id not in superseded_ids]
        if not current:
            return None
        return max(current, key=lambda run: run.attempt_number).id
