"""Allow term_triage_decisions.rationale to be genuinely absent.

`TermTriageDecision.rationale` is `str | None` in the canonical Pydantic
contract, but migration 0013's shared table-creation loop inverted its
`optional` membership check (`nullable=(c not in optional)` instead of
`nullable=(c in optional)`), leaving `rationale` -- the one column that
loop actually intended to mark optional -- as the only NOT NULL column in
`term_triage_decisions`. A valid accepted triage result with no rationale
could therefore fail during persistence. This migration changes only this
one column; it does not touch the same inversion's effect on any other
column or table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_triage_rationale_nullable"
down_revision = "0013_term_enrichment_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Relax the column to nullable, matching the Pydantic contract exactly."""
    op.alter_column("term_triage_decisions", "rationale", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    """Restore NOT NULL only when safe; never rewrite or discard existing data.

    Aborts with a clear error if any row already has a NULL rationale --
    converting those to an empty string (or any other placeholder) would
    silently change semantic data, which this migration must not do.
    """
    conn = op.get_bind()
    null_count = conn.execute(sa.text("SELECT count(*) FROM term_triage_decisions WHERE rationale IS NULL")).scalar()
    if null_count:
        raise RuntimeError(
            f"Cannot downgrade term_triage_decisions.rationale to NOT NULL: "
            f"{null_count} row(s) have a NULL rationale. This downgrade refuses to "
            "rewrite or discard that data -- resolve those rows first if the NOT NULL "
            "constraint must be restored."
        )
    op.alter_column("term_triage_decisions", "rationale", existing_type=sa.Text(), nullable=False)
