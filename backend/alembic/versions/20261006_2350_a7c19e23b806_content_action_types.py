"""Add explicit resource editing actions for durable HTTP idempotency."""

from collections.abc import Sequence

from alembic import op

revision: str = "a7c19e23b806"
down_revision: str | None = "d31e82a9f647"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'publish', 'revoke', 'delete', 'update_settings', 'provide_input', 'create_memory', 'update_memory', 'delete_memory', 'apply_retention'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_actions_type_valid"), "actions", type_="check")
    op.create_check_constraint("type_valid", "actions", f"type IN ({_OLD}, 'create_resource', 'update_resource')")


def downgrade() -> None:
    # 不能丢弃永久审计或把新动作改名；有新类型数据时 CHECK 拒绝回退。
    op.drop_constraint(op.f("ck_actions_type_valid"), "actions", type_="check")
    op.create_check_constraint("type_valid", "actions", f"type IN ({_OLD})")
