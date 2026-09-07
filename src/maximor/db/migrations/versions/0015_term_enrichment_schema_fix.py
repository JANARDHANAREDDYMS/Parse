"""Reconcile term-enrichment tables with their intended ORM/Pydantic contract.

Migration 0013's shared table-creation loop computed
`nullable=(c not in optional)` where `optional = {"rationale", "raw_period_label"}`
-- an inverted boolean. The two columns actually meant to be optional ended
up the *only* `NOT NULL` columns created by that loop, while every other
column it created (across `term_triage_decisions`, `term_applicability_decisions`,
`candidate_commercial_fact_coverages`, `candidate_commercial_facts`) ended up
nullable when the ORM model requires it. Migration 0014 already corrected
`term_triage_decisions.rationale` in isolation; this migration reconciles
every other affected column, using `alembic check` against the live ORM
models as the source of truth for intended nullability -- never the
accidental 0013 database state.

The same migration also fixes two unrelated drift items introduced by
0013's ad-hoc table construction, confirmed via the same `alembic check`
comparison:

- `organization_id` never received a foreign key to `organizations.id` on
  any of the five term-enrichment projection tables (only `run_id` did);
- `run_id`/`organization_id` (and, on `term_applicability_runs`,
  `analysis_run_id`/`document_id`/`organization_id`/`processing_job_id`)
  never received the indexes their ORM columns declare via `index=True`.

Finally, `candidate_commercial_fact_coverages` and `candidate_commercial_facts`
each carry a `source_order` column that exists in neither the ORM model nor
any Pydantic contract -- an accidental column from 0013's shared loop
copying a shape meant for the other two tables. It is dropped outright;
there is no "intended" value for it to preserve.

Every column tightened to NOT NULL is checked for existing NULL rows first;
the migration aborts with a clear error rather than inventing a default or
rewriting data. `raw_period_label` on `candidate_commercial_facts` is
loosened, not tightened: it is genuinely optional
(`RawCommercialFact.raw_period_label: str | None`) and was the other
casualty of the same inversion bug.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015_term_enrichment_schema_fix"
down_revision = "0014_triage_rationale_nullable"
branch_labels = None
depends_on = None

# Column -> intended type, for every column this migration tightens to NOT NULL.
REQUIRED_NOT_NULL: dict[str, list[str]] = {
    "term_triage_decisions": ["run_id", "organization_id", "term_id", "disposition", "source_order"],
    "term_applicability_decisions": ["run_id", "organization_id", "term_id", "disposition", "candidate_ids", "evidence", "source_order"],
    "candidate_commercial_fact_coverages": ["run_id", "organization_id", "candidate_id", "expected_fields", "extracted_fields", "unresolved_fields", "evidence"],
    "candidate_commercial_facts": ["run_id", "organization_id", "candidate_id", "fact_id", "field", "raw_value", "evidence"],
}

COLUMN_TYPES: dict[str, sa.types.TypeEngine] = {
    "run_id": postgresql.UUID(as_uuid=True),
    "organization_id": postgresql.UUID(as_uuid=True),
    "term_id": sa.String(128),
    "disposition": sa.String(64),
    "source_order": sa.Integer(),
    "candidate_ids": postgresql.JSONB(),
    "evidence": postgresql.JSONB(),
    "candidate_id": sa.String(128),
    "expected_fields": postgresql.JSONB(),
    "extracted_fields": postgresql.JSONB(),
    "unresolved_fields": postgresql.JSONB(),
    "fact_id": sa.String(200),
    "field": sa.String(64),
    "raw_value": sa.Text(),
}

# Every table that needs the missing organization_id FK and run_id/organization_id indexes,
# paired with its FK constraint name. Most follow the naming_convention's own
# "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s" pattern; two
# tables need the same explicit short override the ORM model itself declares,
# because the convention-derived name exceeds Postgres' 63-character identifier limit.
PROJECTION_TABLES_AND_FK_NAMES = (
    ("term_triage_decisions", "fk_term_triage_decisions_organization_id_organizations"),
    ("term_applicability_decisions", "fk_term_applicability_decisions_organization_id_organizations"),
    ("candidate_commercial_fact_coverages", "fk_candidate_fact_coverage_org"),
    ("candidate_commercial_facts", "fk_candidate_commercial_facts_organization_id_organizations"),
    ("term_applicability_evidence_references", "fk_term_evidence_refs_org"),
)


def _abort_if_nulls(conn, table: str, column: str) -> None:
    """Raise a clear, safe error instead of tightening a column that would reject existing rows."""
    null_count = conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {column} IS NULL")).scalar()
    if null_count:
        raise RuntimeError(
            f"Cannot tighten {table}.{column} to NOT NULL: {null_count} row(s) are currently NULL. "
            "This migration refuses to invent a default or discard that data -- resolve those rows "
            "(or accept the current schema) before upgrading."
        )


def upgrade() -> None:
    """Tighten required columns, loosen the genuinely optional one, and add missing FKs/indexes."""
    conn = op.get_bind()

    for table, columns in REQUIRED_NOT_NULL.items():
        for column in columns:
            _abort_if_nulls(conn, table, column)

    for table, columns in REQUIRED_NOT_NULL.items():
        for column in columns:
            op.alter_column(table, column, existing_type=COLUMN_TYPES[column], nullable=False)

    op.alter_column("candidate_commercial_facts", "raw_period_label", existing_type=sa.String(100), nullable=True)

    op.drop_column("candidate_commercial_fact_coverages", "source_order")
    op.drop_column("candidate_commercial_facts", "source_order")

    for table, fk_name in PROJECTION_TABLES_AND_FK_NAMES:
        op.create_foreign_key(
            fk_name, table, "organizations",
            ["organization_id"], ["id"], ondelete="CASCADE",
        )
        op.create_index(f"ix_{table}_run_id", table, ["run_id"])
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])

    op.create_index("ix_term_applicability_runs_analysis_run_id", "term_applicability_runs", ["analysis_run_id"])
    op.create_index("ix_term_applicability_runs_document_id", "term_applicability_runs", ["document_id"])
    op.create_index("ix_term_applicability_runs_organization_id", "term_applicability_runs", ["organization_id"])
    op.create_index("ix_term_applicability_runs_processing_job_id", "term_applicability_runs", ["processing_job_id"])


def downgrade() -> None:
    """Restore the pre-0015 shape exactly, refusing to tighten `raw_period_label` unsafely.

    Dropping the missing indexes/foreign keys and re-loosening the
    previously-tightened columns is always safe. Re-adding
    `source_order` restores the column's presence, not its historical
    values -- those never had any meaning outside 0013's accidental
    construction and the column could not have been populated with
    anything meaningful while 0015 was active (it did not exist then).
    """
    conn = op.get_bind()

    # Fail fast, before any DDL, if restoring NOT NULL on raw_period_label
    # would reject rows an 0015-era caller legitimately left it absent for.
    _abort_if_nulls(conn, "candidate_commercial_facts", "raw_period_label")

    op.drop_index("ix_term_applicability_runs_processing_job_id", table_name="term_applicability_runs")
    op.drop_index("ix_term_applicability_runs_organization_id", table_name="term_applicability_runs")
    op.drop_index("ix_term_applicability_runs_document_id", table_name="term_applicability_runs")
    op.drop_index("ix_term_applicability_runs_analysis_run_id", table_name="term_applicability_runs")

    for table, fk_name in PROJECTION_TABLES_AND_FK_NAMES:
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        op.drop_index(f"ix_{table}_run_id", table_name=table)
        op.drop_constraint(fk_name, table, type_="foreignkey")

    op.add_column("candidate_commercial_facts", sa.Column("source_order", sa.Integer(), nullable=True))
    op.add_column("candidate_commercial_fact_coverages", sa.Column("source_order", sa.Integer(), nullable=True))

    op.alter_column("candidate_commercial_facts", "raw_period_label", existing_type=sa.String(100), nullable=False)

    for table, columns in REQUIRED_NOT_NULL.items():
        for column in columns:
            op.alter_column(table, column, existing_type=COLUMN_TYPES[column], nullable=True)
