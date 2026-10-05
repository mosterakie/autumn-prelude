"""repositories 模块。

原子状态转换 API：SQL / UPSERT / CAS / 行锁；DB 约束错误映射为领域错误。
硬约束：不是通用 CRUD 层；允许 flush，禁止 commit。

已实现事务绑定、能力基类、Run/Event、Quota、Job 和 ProviderCall 原子方法。
实施进度见 backend/DEVELOPMENT.md。
"""
