"""Add indexes declared by preprocessing projection models."""
from alembic import op
revision='0003_preprocessing_indexes'
down_revision='0002_document_preprocessing'
branch_labels=None
depends_on=None
def upgrade():
    for table, columns in [('document_processing_runs',['organization_id','document_id','processing_job_id']),('document_pages',['organization_id','processing_run_id']),('document_blocks',['organization_id','processing_run_id','document_page_id']),('document_tables',['organization_id','processing_run_id','document_page_id'])]:
        for column in columns: op.create_index(f'ix_{table}_{column}',table,[column])
def downgrade():
    for table, columns in [('document_tables',['document_page_id','processing_run_id','organization_id']),('document_blocks',['document_page_id','processing_run_id','organization_id']),('document_pages',['processing_run_id','organization_id']),('document_processing_runs',['processing_job_id','document_id','organization_id'])]:
        for column in columns: op.drop_index(f'ix_{table}_{column}',table_name=table)
