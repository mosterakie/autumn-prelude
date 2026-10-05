"""policies 模块。

纯权限判定：ActorContext + PolicyFacts -> Decision。
硬约束：判定函数零 I/O，可脱离数据库单测；可选 facts loader 不得演变成第二个 service。

当前阶段：A1 仅建立包布局与依赖规则，尚未实现具体内容。
"""
