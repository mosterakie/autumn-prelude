"""services 模块。

应用流程与 UoW 边界：content / publication / run / quota / action / knowledge。
硬约束：一个操作可包含多个短 UoW，但任何 UoW 都不得跨 LLM / 搜索 / 对象存储 / 邮件。

当前阶段：E1 发布/撤回应用事务已实现，其余业务按实施顺序继续。
"""
