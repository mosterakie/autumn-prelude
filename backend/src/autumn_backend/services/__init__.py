"""services 模块。

应用流程与 UoW 边界：content / publication / run / quota / action / knowledge。
硬约束：一个操作可包含多个短 UoW，但任何 UoW 都不得跨 LLM / 搜索 / 对象存储 / 邮件。

当前阶段：A1 仅建立包布局与依赖规则，尚未实现具体内容。
"""
