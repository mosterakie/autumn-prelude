"""jobs 模块。

持久队列基础设施：enqueue / claim / heartbeat / finish / reclaim。
硬约束：不认识 services 或任何业务 handler（消除循环依赖）。

当前阶段：A1 仅建立包布局与依赖规则，尚未实现具体内容。
"""
