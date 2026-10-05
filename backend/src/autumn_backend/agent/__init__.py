"""agent 模块。

Agent Runtime：LangGraph 工作流、上下文与预算、checkpoint、恢复；tools 为 service 薄适配器。
硬约束：不依赖 workers，不直接绕过 service；ActorContext 只能由服务端注入。

当前阶段：A1 仅建立包布局与依赖规则，尚未实现具体内容。
"""
