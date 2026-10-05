"""api 模块。

HTTP 入口：routers / schemas / deps / errors / SSE。
硬约束：不直接访问 ORM，不直接调 provider；仅依赖 services / auth / observability。

当前阶段：A1 仅建立包布局与依赖规则，尚未实现具体内容。
"""
