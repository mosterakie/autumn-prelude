"""Persist the run execution generation used by service result fencing."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d31e82a9f647"
down_revision: str | None = "b8a71e06d204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "execution_generation", sa.BigInteger(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.create_check_constraint("execution_generation_positive", "runs", "execution_generation >= 1")
    op.add_column(
        "run_sources",
        sa.Column(
            "context_generation", sa.BigInteger(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.create_check_constraint(
        "context_generation_positive", "run_sources", "context_generation >= 1"
    )
    op.execute("""UPDATE jobs AS j SET payload = coalesce(j.payload, '{}'::jsonb) || jsonb_build_object('execution_generation', r.execution_generation)
        FROM runs AS r WHERE j.run_id = r.id AND j.kind IN ('run.dispatch', 'run.resume', 'action.execute')
        AND (NOT coalesce(j.payload, '{}'::jsonb) ? 'execution_generation' OR j.payload->'execution_generation' = 'null'::jsonb)""")


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_run_sources_context_generation_positive"), "run_sources", type_="check"
    )
    op.drop_column("run_sources", "context_generation")
    op.drop_constraint(op.f("ck_runs_execution_generation_positive"), "runs", type_="check")
    op.drop_column("runs", "execution_generation")
