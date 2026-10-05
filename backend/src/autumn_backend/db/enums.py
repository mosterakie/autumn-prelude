"""数据库级受控词表（枚举）。

为什么用 ``VARCHAR + CHECK`` 而不是 PostgreSQL 原生 ``ENUM``：

- 原生 ENUM 增删值需要 ``ALTER TYPE``，``alembic --autogenerate`` 无法可靠表达，
  会让 A4 的 ``alembic check`` 出现漂移；
- 文档要求"主状态保持有限集合，具体原因用 error_code 表达"，
  受控词表应当能被迁移显式修改，而不是隐式依赖类型系统；
- ``values_callable`` 保证写入的是**值**而不是 ``UserRole.MEMBER`` 这种成员名，
  避免"Python 改了成员名、数据库里字符串没变"的静默分裂。
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from sqlalchemy import Enum as SAEnum

__all__ = [
    "ActionKind",
    "ActionStatus",
    "AdminFactorType",
    "AuthTokenPurpose",
    "CommentStatus",
    "ContentFormat",
    "ConversationMode",
    "EventType",
    "IndexKind",
    "IndexSourceKind",
    "JobStatus",
    "MessageRole",
    "ModelProfile",
    "ProviderCallPurpose",
    "ProviderCallStatus",
    "PublicationRevokeReason",
    "QuotaReservationStatus",
    "ResourceType",
    "Role",
    "RunSourceKind",
    "RunStatus",
    "SettingValueType",
    "enum_check_expression",
    "enum_column_type",
    "enum_values",
    "in_predicate",
]


class Role(enum.StrEnum):
    """主体角色。``anonymous`` 不落库，只在 ActorContext 中出现。"""

    MEMBER = "member"
    OWNER = "owner"


class AuthTokenPurpose(enum.StrEnum):
    """一次性令牌用途。"""

    EMAIL_VERIFY = "email_verify"
    PASSWORD_RESET = "password_reset"
    STEP_UP = "step_up"
    ADMIN_BOOTSTRAP = "admin_bootstrap"


class AdminFactorType(enum.StrEnum):
    """站长额外验证因子。"""

    TOTP = "totp"
    RECOVERY_CODE = "recovery_code"


class SettingValueType(enum.StrEnum):
    """``settings.value`` 的类型标签。

    值列用 JSONB 存放，类型标签让 service 明确知道该怎么解释它，
    也便于后续做配置校验。
    """

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    JSON = "json"


class ResourceType(enum.StrEnum):
    """资源类型白名单。

    发布投影按类型白名单构造（架构文档 §5.1），因此这里的取值同时决定了
    "哪些字段可以公开"。新增类型必须同时定义它的公开投影。
    """

    ARTICLE = "article"
    WEB_PAGE = "web_page"
    FILE = "file"


class ContentFormat(enum.StrEnum):
    """``resource_versions`` 中正文的存储格式。"""

    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"


class CommentStatus(enum.StrEnum):
    """评论状态。

    主状态保持有限集合：撤下原因用 ``moderated_reason`` 之类的附加字段表达，
    不为每种原因扩张状态集合。
    """

    VISIBLE = "visible"
    HIDDEN = "hidden"


class PublicationRevokeReason(enum.StrEnum):
    """撤销发布的原因。"""

    REPUBLISHED = "republished"
    OWNER_REVOKED = "owner_revoked"
    ADMIN_TAKEDOWN = "admin_takedown"
    RESOURCE_DELETED = "resource_deleted"


class IndexKind(enum.StrEnum):
    """知识索引种类。"""

    PRIVATE = "private"
    PUBLIC = "public"


class IndexSourceKind(enum.StrEnum):
    """索引对象直接从哪一类投影构建。

    **公开索引只能从公开投影构建**（架构文档 §9.2）：
    本枚举与 :class:`IndexKind` 一起被 CHECK 约束绑定到具体外键列，
    使"复用私人 chunk 再加 public 标记"在结构上无法表达。
    """

    RESOURCE_VERSION = "resource_version"
    PUBLICATION = "publication"


class RunSourceKind(enum.StrEnum):
    """进入模型的来源是私人语料还是公开语料。"""

    PRIVATE = "private"
    PUBLIC = "public"


class ConversationMode(enum.StrEnum):
    """会话的固定模式。

    一个 conversation 固定 ``public`` 或 ``owner``，**不随单次提问改变**：
    ``thread_id`` 由服务端生成并绑定 conversation + mode，checkpoint 只是恢复依据，
    不是权限来源（架构文档 §6.1）。
    """

    PUBLIC = "public"
    OWNER = "owner"


class MessageRole(enum.StrEnum):
    """消息角色。``messages`` 存完整消息文本；``run_events`` 只存事件与元数据。"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class RunStatus(enum.StrEnum):
    """Run 主状态：刻意保持有限集合。

    具体终止原因用 ``error_code`` / ``terminal_reason`` 表达，
    不为每种原因扩张状态集合（架构文档 §6.2）。

    非终态集合见 :data:`NON_TERMINAL_RUN_STATUSES`——Partial Unique Index
    "同 conversation 只有一个非终态 run" 依赖它。
    """

    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 非终态集合。必须以字面量形式出现在 Partial Unique Index 谓词里，
#: 因此这里既是语义定义，也是迁移的输入。
NON_TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    RunStatus.PENDING.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_INPUT.value,
)

#: 终态集合。与上者互补且不可重叠。
TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
)


class ModelProfile(enum.StrEnum):
    """模型档位：由服务端决定，不由模型/工具参数决定。"""

    PRIMARY = "primary"
    FAST = "fast"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class EventType(enum.StrEnum):
    """``run_events`` 的事件类型白名单。

    受控词表而非自由字符串：新增事件类型必须显式改迁移，
    避免前端契约在无人察觉的情况下漂移。
    """

    RUN_STARTED = "run.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_SNAPSHOT = "message.snapshot"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    SOURCE_RECORDED = "source.recorded"
    ACTION_REQUIRED = "action.required"
    RUN_INVALIDATED = "run.invalidated"
    RUN_TERMINAL = "run.terminal"


class QuotaReservationStatus(enum.StrEnum):
    """配额预留状态机。**任何转换都不倒退**（Repository 文档 §7.3）。

    - ``reserved``：已接受、尚未调用供应商
    - ``charged``：首次进入供应商阶段
    - ``released``：供应商调用前取消/本地失败
    - ``refunded``：确认的服务端/供应商故障补偿
    """

    RESERVED = "reserved"
    CHARGED = "charged"
    RELEASED = "released"
    REFUNDED = "refunded"


class JobStatus(enum.StrEnum):
    """持久队列任务状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProviderCallStatus(enum.StrEnum):
    """外部调用受限状态机。

    ``unknown`` 用于网络超时或无法确认远端最终状态，
    **不能**简单当作"失败且费用为 0"（Repository 文档 §9）。
    """

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ProviderCallPurpose(enum.StrEnum):
    """外部调用的用途：决定成本归类与对账方式。"""

    CHAT = "chat"
    TOOL = "tool"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    SEARCH = "search"
    FETCH = "fetch"
    EMAIL = "email"


class ActionKind(enum.StrEnum):
    """需要用户确认的持久等待动作类型。"""

    PUBLISH = "publish"
    REVOKE = "revoke"
    DELETE = "delete"
    UPDATE_SETTINGS = "update_settings"
    PROVIDE_INPUT = "provide_input"


class ActionStatus(enum.StrEnum):
    """动作状态机。``expired`` 是终态之一，但主状态集合仍保持有限。"""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """枚举的**值**列表（不是成员名）。"""
    return [str(member.value) for member in enum_class]


def enum_check_expression(column: str, enum_class: type[enum.Enum]) -> str:
    """生成 ``column IN ('a', 'b')`` 形式的 CHECK 表达式。

    表达式里的字面量只来自代码里的枚举定义，不含任何外部输入。
    """
    values = enum_values(enum_class)
    if not values:
        raise ValueError(f"{enum_class.__name__} 没有任何值，无法生成 CHECK 约束")
    literals = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({literals})"


def enum_column_type(enum_class: type[enum.Enum], *, length: int | None = None) -> SAEnum:
    """生成与受控词表配套的 ``Enum`` 列类型（``native_enum=False``）。"""
    return SAEnum(
        enum_class,
        native_enum=False,
        create_constraint=False,  # CHECK 由各模型显式命名，避免自动名不符合命名约定
        length=length,
        validate_strings=True,
        values_callable=enum_values,
    )


def in_predicate(column: str, values: Iterable[str]) -> str:
    """生成 ``column IN ('a', 'b')`` 形式的**部分索引谓词**。

    与 :func:`enum_check_expression` 的区别：这里接受任意字符串序列（例如
    ``NON_TERMINAL_RUN_STATUSES``），用于 ``Index(postgresql_where=...)``。
    谓词必须是不可变表达式：不能含子查询，也不能依赖 ``now()``。

    字面量只来自代码里的常量定义，不含任何外部输入。
    """
    items = list(values)
    if not items:
        raise ValueError("部分索引谓词至少要有一个取值")
    literals = ", ".join(f"'{item}'" for item in items)
    return f"{column} IN ({literals})"
