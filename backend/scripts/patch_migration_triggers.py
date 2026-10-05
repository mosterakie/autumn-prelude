"""一次性工具：给重新生成的迁移补上 ``updated_at`` 触发器与函数创建。

autogenerate 不涉及触发器，因此每次重生成基线后都要补这一段。
本脚本按批次表清单插入：

- 每个 ``upgrade()`` 开头：``CREATE OR REPLACE FUNCTION``（幂等）
- 每个 ``upgrade()`` 末尾：为带 ``updated_at`` 的表逐条挂触发器
- 每个 ``downgrade()`` 开头：逐条删除触发器
- 批次一的 ``downgrade()`` 末尾：删除触发器函数

用完即删；正常迁移流程不需要它。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERSIONS = BACKEND_ROOT / "alembic" / "versions"

#: 批次 -> 表清单（顺序即建表顺序）。
BATCHES: dict[str, tuple[str, ...]] = {
    "identity": (
        "users",
        "auth_sessions",
        "auth_tokens",
        "admin_factors",
        "rate_limit_buckets",
        "settings",
    ),
    "content": (
        "retention_policies",
        "resources",
        "file_objects",
        "resource_versions",
        "publications",
        "comments",
        "reports",
    ),
    "runtime": (
        "conversations",
        "runs",
        "messages",
        "run_events",
        "actions",
        "quota_buckets",
        "quota_reservations",
        "jobs",
        "provider_calls",
        "audit_events",
    ),
    "knowledge": (
        "knowledge_indexes",
        "knowledge_chunks",
        "run_sources",
        "conversation_summaries",
        "memories",
    ),
}

#: 只有 created_at 的只追加表，不挂触发器。
_APPEND_ONLY = {"run_events", "audit_events", "knowledge_chunks", "run_sources"}

_IMPORT_BLOCK = """from autumn_backend.db.timestamps import (
    CREATE_UPDATED_AT_FUNCTION_SQL,
    DROP_UPDATED_AT_FUNCTION_SQL,
    drop_updated_at_trigger_statements,
    updated_at_trigger_statements,
)
"""

_TRIGGER_CREATE = """
    # 为每张带 updated_at 的表挂触发器（autogenerate 不涉及触发器）。
    # 每条 DDL 必须独立执行：asyncpg 不接受一次预编译多条命令。
    for table_name in _TIMESTAMPED_TABLES:
        for statement in updated_at_trigger_statements(table_name):
            op.execute(statement)
"""

_TRIGGER_DROP = """    for table_name in _TIMESTAMPED_TABLES:
        for statement in drop_updated_at_trigger_statements(table_name):
            op.execute(statement)

"""


def _tables_in(file_text: str) -> set[str]:
    # ruff format 会把单引号改成双引号，因此两种引号都要匹配。
    return set(re.findall(r"""op\.create_table\(\s*['"]([a-z_]+)['"]""", file_text))


def _batch_for(tables: set[str]) -> str:
    for name, expected in BATCHES.items():
        if tables == set(expected):
            return name
    raise SystemExit(f"无法识别迁移批次，表集合为：{sorted(tables)}")


def patch(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    tables = _tables_in(text)
    batch = _batch_for(tables)
    timestamped = tuple(t for t in BATCHES[batch] if t not in _APPEND_ONLY)

    if "_TIMESTAMPED_TABLES" in text:
        return f"{path.name}: 已打过补丁，跳过"

    # 1) 补导入。
    anchor = "from sqlalchemy.dialects import postgresql\n"
    if anchor not in text:
        anchor = "from alembic import op\n"
    extra = ""
    if "pgvector." in text and "import pgvector" not in text:
        # Vector 列在迁移里以 ``pgvector.sqlalchemy.vector.VECTOR`` 形式出现，
        # 因此必须显式导入该模块。
        extra = "import pgvector.sqlalchemy\n"
    text = text.replace(anchor, extra + anchor + "\n" + _IMPORT_BLOCK, 1)

    # 2) 补常量。
    marker = "depends_on: str | Sequence[str] | None = None\n"
    constant = (
        marker
        + "\n#: 本批需要挂 updated_at 触发器的表（只追加表不在其中）。\n"
        + "_TIMESTAMPED_TABLES: tuple[str, ...] = (\n"
        + "".join(f'    "{name}",\n' for name in timestamped)
        + ")\n"
    )
    text = text.replace(marker, constant, 1)

    # 3) upgrade 开头补函数创建。
    text = text.replace(
        "def upgrade() -> None:\n",
        "def upgrade() -> None:\n"
        "    # 幂等：函数可能已由前批迁移创建。\n"
        "    op.execute(CREATE_UPDATED_AT_FUNCTION_SQL)\n\n",
        1,
    )

    # 4) upgrade 末尾补触发器挂载（插在第一个 def downgrade 之前）。
    text = text.replace(
        "\ndef downgrade() -> None:\n", _TRIGGER_CREATE + "\ndef downgrade() -> None:\n", 1
    )

    # 5) downgrade 开头补触发器删除。
    text = text.replace(
        "def downgrade() -> None:\n", "def downgrade() -> None:\n" + _TRIGGER_DROP, 1
    )

    # 6) 批次一在 downgrade 末尾删除函数。
    if batch == "identity":
        text = text.rstrip("\n") + (
            "\n\n    # 所有表都删完之后才可移除触发器函数。\n"
            "    op.execute(DROP_UPDATED_AT_FUNCTION_SQL)\n"
        )

    path.write_text(text, encoding="utf-8")
    return f"{path.name}: 批次 {batch}，触发器表 {timestamped}"


def main() -> int:
    files = sorted(VERSIONS.glob("*.py"))
    if not files:
        print("没有迁移文件", file=sys.stderr)
        return 1
    for path in files:
        print(patch(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
