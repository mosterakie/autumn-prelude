"""tests.integration：需要真实 PostgreSQL 的集成测试。

用于 Repository 原子状态转换、Service 事务边界、Alembic 迁移与约束存在性。
**禁止**用 SQLite 或 Mock Repository 替代：无法证明 ON CONFLICT / FOR UPDATE /
SKIP LOCKED / Partial Unique Index / 隔离级别。
"""
