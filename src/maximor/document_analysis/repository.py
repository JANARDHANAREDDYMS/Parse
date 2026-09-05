"""Query tenant-scoped persisted preprocessing projections for narrow read-only tools.

Inputs are typed scopes and query values. Outputs are database records or safe
absence; this repository performs parameterized SQLAlchemy queries only and exposes
neither SQL nor sessions to the agent-facing tool schemas.
"""
import uuid
from sqlalchemy import cast, select, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from maximor.db.models import DocumentBlock, DocumentPage, DocumentProcessingRun, DocumentTable

class DocumentAnalysisRepository:
    """Provide completed-run, page, block, and table lookups scoped by organization."""
    def __init__(self, sessions:async_sessionmaker[AsyncSession]) -> None:
        """Receive the application session factory; callers never receive a session."""
        self._sessions=sessions
    async def run(self, organization_id:uuid.UUID, run_id:uuid.UUID):
        """Return one tenant-scoped run regardless of status."""
        async with self._sessions() as s: return await s.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.id==run_id,DocumentProcessingRun.organization_id==organization_id))
    async def page(self, organization_id:uuid.UUID, run_id:uuid.UUID, page_number:int):
        """Return one page that belongs to the tenant-scoped processing run."""
        async with self._sessions() as s: return await s.scalar(select(DocumentPage).where(DocumentPage.organization_id==organization_id,DocumentPage.processing_run_id==run_id,DocumentPage.page_number==page_number))
    async def pages(self, organization_id:uuid.UUID, run_id:uuid.UUID):
        """Return pages in deterministic ascending page order."""
        async with self._sessions() as s: return list((await s.scalars(select(DocumentPage).where(DocumentPage.organization_id==organization_id,DocumentPage.processing_run_id==run_id).order_by(DocumentPage.page_number))).all())
    async def blocks(self, organization_id, run_id, page_number, representation, limit, block_type=None):
        """Return bounded blocks in deterministic reading order."""
        stmt=select(DocumentBlock).join(DocumentPage,DocumentBlock.document_page_id==DocumentPage.id).where(DocumentBlock.organization_id==organization_id,DocumentBlock.processing_run_id==run_id,DocumentPage.page_number==page_number,DocumentBlock.representation==representation)
        if block_type: stmt=stmt.where(DocumentBlock.block_type==block_type)
        stmt=stmt.order_by(DocumentBlock.reading_order,DocumentBlock.external_block_id).limit(limit)
        async with self._sessions() as s: return list((await s.scalars(stmt)).all())
    async def tables(self, organization_id, run_id, page_number, limit):
        """Return bounded tables in ascending persisted table index order."""
        stmt=select(DocumentTable).join(DocumentPage,DocumentTable.document_page_id==DocumentPage.id).where(DocumentTable.organization_id==organization_id,DocumentTable.processing_run_id==run_id,DocumentPage.page_number==page_number).order_by(DocumentTable.table_index,DocumentTable.external_table_id).limit(limit)
        async with self._sessions() as s: return list((await s.scalars(stmt)).all())
    async def search_blocks(self, organization_id, run_id, query, page_number, representations, limit):
        """Perform parameterized case-insensitive deterministic substring search on block text."""
        stmt=select(DocumentBlock,DocumentPage.page_number).join(DocumentPage,DocumentBlock.document_page_id==DocumentPage.id).where(DocumentBlock.organization_id==organization_id,DocumentBlock.processing_run_id==run_id,DocumentBlock.representation.in_(representations),DocumentBlock.text.ilike(f'%{query}%'))
        if page_number: stmt=stmt.where(DocumentPage.page_number==page_number)
        stmt=stmt.order_by(DocumentPage.page_number,DocumentBlock.reading_order,DocumentBlock.external_block_id).limit(limit)
        async with self._sessions() as s: return list((await s.execute(stmt)).all())
    async def search_tables(self, organization_id, run_id, query, page_number, limit):
        """Perform parameterized deterministic substring search on stored table JSON."""
        stmt=select(DocumentTable,DocumentPage.page_number).join(DocumentPage,DocumentTable.document_page_id==DocumentPage.id).where(DocumentTable.organization_id==organization_id,DocumentTable.processing_run_id==run_id,cast(DocumentTable.rows,Text).ilike(f'%{query}%'))
        if page_number: stmt=stmt.where(DocumentPage.page_number==page_number)
        stmt=stmt.order_by(DocumentPage.page_number,DocumentTable.table_index,DocumentTable.external_table_id).limit(limit)
        async with self._sessions() as s: return list((await s.execute(stmt)).all())
    async def block_by_id(self, organization_id, run_id, page_number, block_id):
        """Resolve one exact block ID within one tenant/run/page."""
        stmt=select(DocumentBlock).join(DocumentPage,DocumentBlock.document_page_id==DocumentPage.id).where(DocumentBlock.organization_id==organization_id,DocumentBlock.processing_run_id==run_id,DocumentPage.page_number==page_number,DocumentBlock.external_block_id==block_id)
        async with self._sessions() as s: return await s.scalar(stmt)
    async def table_by_id(self, organization_id, run_id, page_number, table_id):
        """Resolve one exact table ID within one tenant/run/page."""
        stmt=select(DocumentTable).join(DocumentPage,DocumentTable.document_page_id==DocumentPage.id).where(DocumentTable.organization_id==organization_id,DocumentTable.processing_run_id==run_id,DocumentPage.page_number==page_number,DocumentTable.external_table_id==table_id)
        async with self._sessions() as s: return await s.scalar(stmt)
