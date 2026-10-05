"""policies 模块。

纯权限判定：ActorContext + PolicyFacts -> Decision。
硬约束：判定函数零 I/O，可脱离数据库单测；可选 facts loader 不得演变成第二个 service。

当前阶段：D，核心只依赖 Python 标准库。认证入口在 F/G/H 装配服务端身份与事实。
"""

from autumn_backend.policies.actor import ActorContext, ActorRole, Capability
from autumn_backend.policies.decision import Decision, DenialCode
from autumn_backend.policies.facts import PolicyFacts
from autumn_backend.policies.policy import evaluate

__all__ = [
    "ActorContext",
    "ActorRole",
    "Capability",
    "Decision",
    "DenialCode",
    "PolicyFacts",
    "evaluate",
]
