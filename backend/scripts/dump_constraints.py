"""开发期辅助：把元数据里的 CHECK / 唯一约束 / 部分索引导成精确 SQL 字面量。

迁移脚本**不能**在运行期导入应用代码来生成 CHECK 表达式——那会让"历史迁移"
随应用代码变化而改变含义。因此写入迁移的字面量必须与这里的输出逐字一致，
再由 catalog 核对测试闭环验证。
"""

from __future__ import annotations

import sys

from sqlalchemy import CheckConstraint, UniqueConstraint

from autumn_backend.db.base import Base
from autumn_backend.db.models import load_all_models


def main(tables: list[str]) -> int:
    load_all_models()
    for name in tables:
        table = Base.metadata.tables[name]
        print(f"===== {name} =====")
        for constraint in sorted(table.constraints, key=lambda c: (type(c).__name__, c.name or "")):
            if isinstance(constraint, CheckConstraint) and constraint.name:
                print(f"CHECK {constraint.name}")
                print(f"      {constraint.sqltext}")
            elif isinstance(constraint, UniqueConstraint) and constraint.name:
                cols = ", ".join(c.name for c in constraint.columns)
                print(f"UNIQUE {constraint.name} ({cols})")
        for index in table.indexes:
            if index.name is None:
                continue
            where = index.dialect_options["postgresql"].get("where")
            cols = ", ".join(c.name for c in index.columns)
            suffix = f" WHERE {where}" if where is not None else ""
            unique = "UNIQUE " if index.unique else ""
            print(f"{unique}INDEX {index.name} ({cols}){suffix}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
