"""能力展示矩阵。来源是当前认证事实，不信任 Actor 中缓存的提示。"""

from autumn_backend.policies.access import authentication_denial, owner_denial, verified_denial
from autumn_backend.policies.actor import ActorContext, Capability
from autumn_backend.policies.facts import PolicyFacts

PUBLIC_CAPABILITIES = frozenset({Capability.READ_PUBLIC})
AUTHENTICATED_CAPABILITIES = PUBLIC_CAPABILITIES | {Capability.OWN_CHAT}
VERIFIED_CAPABILITIES = AUTHENTICATED_CAPABILITIES | {
    Capability.PUBLIC_AI,
    Capability.WRITE_COMMENT,
}
OWNER_CAPABILITIES = VERIFIED_CAPABILITIES | {
    Capability.PRIVATE_KNOWLEDGE,
    Capability.SEARCH_WEB,
    Capability.MANAGE_CONTENT,
    Capability.MANAGE_SITE,
    Capability.OWN_ACTION,
    Capability.OWN_MEMORY,
}


def capabilities_for(actor: ActorContext, facts: PolicyFacts) -> frozenset[Capability]:
    """展示身份能力；不代表余额、冷却、对象归属、确认或业务状态校验通过。"""
    if authentication_denial(actor, facts) is not None:
        return PUBLIC_CAPABILITIES
    if verified_denial(actor, facts) is not None:
        return AUTHENTICATED_CAPABILITIES
    if owner_denial(actor, facts) is None:
        return OWNER_CAPABILITIES
    return VERIFIED_CAPABILITIES
