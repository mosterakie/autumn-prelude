"""允许存在于数据库中、但不属于应用模型的表。

政策放在应用包里（而不是 ``alembic/env.py``），这样迁移运行器与核对测试
引用同一份定义，不会出现"迁移放过了、测试却不知道"的分裂。

**必须逐项登记**，而不是用"排除所有反射到的表"的方式一并放过——
后者会让"应用表意外从 metadata 遗漏"也不被 ``alembic check`` 报告，
等于放弃了一部分表结构漂移保护。

当前为空：pgvector 只提供类型，不建表；框架状态表尚未引入。
将来引入 LangGraph checkpoint 一类外部对象时在这里补条目。
"""

from __future__ import annotations

#: ``(schema, table)`` 形式的允许列表。
EXTERNAL_TABLE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()
