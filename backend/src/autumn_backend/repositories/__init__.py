"""repositories 模块。

原子状态转换 API：SQL / UPSERT / CAS / 行锁；DB 约束错误映射为领域错误。
硬约束：不是通用 CRUD 层；允许 flush，禁止 commit。

当前阶段：A1 仅建立包布局与依赖规则，尚未实现具体内容。
"""
