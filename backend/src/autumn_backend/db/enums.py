"""数据库级受控词表（枚举）。

取值来源与冻结依据：

- 状态、可见性与角色取值来自 ``docs/architecture/database.md``；
- 对外暴露的状态集合（run / job / action / comment）与 ``docs/architecture/api-contract.md``
  的 DTO **同名同值**，避免"DB 一套、DTO 一套"的映射负担与漂移；
- 事件类型来自接口契约的 SSE 事件表。

为什么用 ``VARCHAR + 显式命名 CHECK`` 而不是 PostgreSQL 原生 ``ENUM``：

- 原生 ENUM 增删值需要 ``ALTER TYPE``，``alembic --autogenerate`` 无法可靠表达，
  会让 ``alembic check`` 出现漂移；
- 受控词表应当能被迁移显式修改，而不是隐式依赖类型系统；
- ``values_callable`` 保证写入的是**值**而不是成员名。
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from sqlalchemy import Enum as SAEnum

__all__ = [
    "NON_TERMINAL_RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "ActionAuthorizationKind",
    "ActionStatus",
    "ActionType",
    "AdminFactorKind",
    "AuditResult",
    "AuthTokenPurpose",
    "CommentStatus",
    "ContentFormat",
    "ConversationMode",
    "FileObjectStatus",
    "IndexScope",
    "IndexStatus",
    "JobPhase",
    "JobStatus",
    "MemoryKind",
    "MessageRole",
    "MessageStatus",
    "ProviderCallPurpose",
    "ProviderCallStatus",
    "QuotaReservationStatus",
    "ReportStatus",
    "ResourceKind",
    "RetentionAnchor",
    "RetentionMode",
    "RetentionScope",
    "RunEventType",
    "RunSourceType",
    "RunStatus",
    "SummaryStatus",
    "UserRole",
    "UserStatus",
    "enum_check_expression",
    "enum_column_type",
    "enum_values",
    "in_predicate",
]


# --------------------------------------------------------------- 身份会话 --
class UserRole(enum.StrEnum):
    """角色。公开注册强制 ``member``；``owner`` 只能由受控引导或管理流程授予。"""

    MEMBER = "member"
    OWNER = "owner"


class UserStatus(enum.StrEnum):
    """账号状态。

    ``pending_verification`` 表示尚未完成邮箱验证；
    ``disabled`` 必须能阻断待执行工具与后续流式输出。
    """

    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    DISABLED = "disabled"


class AuthTokenPurpose(enum.StrEnum):
    """一次性链接令牌的用途。

    刻意只保留文档定义的两个值：step-up 由会话字段承担，
    站长引导走本地 CLI 流程，都不需要落库的一次性链接。
    """

    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"


class AdminFactorKind(enum.StrEnum):
    """站长额外验证因子类型。文档固定为 TOTP；恢复码是它的一个字段而非另一种因子。"""

    TOTP = "totp"


# ----------------------------------------------------------------- 内容 ----
class ResourceKind(enum.StrEnum):
    """资源类型白名单。同一资源的 ``kind`` 创建后不可改。"""

    ARTICLE = "article"
    BOOKMARK = "bookmark"
    DOCUMENT = "document"
    WEBPAGE = "webpage"


class ContentFormat(enum.StrEnum):
    """版本正文的存储格式。"""

    MARKDOWN = "markdown"
    PLAIN = "plain"


class CommentStatus(enum.StrEnum):
    """留言状态。公开读取要求 ``approved`` 且未删除。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    HIDDEN = "hidden"


class ReportStatus(enum.StrEnum):
    """举报处理状态。每个用户对同一留言最多一条 ``open`` 举报。"""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class FileObjectStatus(enum.StrEnum):
    """对象存储中的文件生命周期。

    阶段 E5 的 staging / finalize / delete 流程必须落在**可变**的文件对象记录上，
    而不是反复修改声明不可变的 ``resource_versions``。
    物理 I/O 不参与数据库事务，因此状态推进靠 job 收敛。
    """

    STAGED = "staged"
    READY = "ready"
    PENDING_DELETE = "pending_delete"
    FAILED = "failed"


# ----------------------------------------------------------------- 索引 ----
class IndexScope(enum.StrEnum):
    """知识索引作用域。``public`` 必须绑定当时的 publication。"""

    OWNER = "owner"
    PUBLIC = "public"


class IndexStatus(enum.StrEnum):
    """索引构建状态。查询只使用 ``ready`` 且活动的索引。"""

    QUEUED = "queued"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    RETIRED = "retired"


# ----------------------------------------------------------------- 对话 ----
class ConversationMode(enum.StrEnum):
    """会话的固定模式；创建后不可修改。``owner`` 模式必须属于站长。"""

    PUBLIC = "public"
    OWNER = "owner"


class MessageRole(enum.StrEnum):
    """消息角色。工具协议消息保存在运行状态中，**不混入**普通对话展示。"""

    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(enum.StrEnum):
    """消息状态。正文累计更新只允许发生在正在生成的 assistant 消息上。"""

    COMPOSING = "composing"
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    HIDDEN = "hidden"


class RunStatus(enum.StrEnum):
    """Run 主状态：数量有限，且与 API 契约的 ``status`` 枚举完全一致。

    具体终止原因用 ``error_code`` 表达，不为每种原因扩张状态集合。
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_AUTH = "waiting_auth"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


#: 非终态集合。以字面量出现在 Partial Unique Index 谓词里，因此这里既是语义定义也是迁移输入。
#: ``cancelling`` 属于非终态：取消已受理但尚未落定。
NON_TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_INPUT.value,
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.WAITING_AUTH.value,
    RunStatus.CANCELLING.value,
)

#: 终态集合；与上者互补且不可重叠。
TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
)


class RunSourceType(enum.StrEnum):
    """进入模型的来源类型。

    ``web`` 类型禁止填入无意义的资源外键，且只有站长运行可创建。
    """

    RESOURCE = "resource"
    WEB = "web"


class RunEventType(enum.StrEnum):
    """``run_events.type`` 白名单。

    与接口契约第 5 节的 SSE 事件表**同名**：事件回放与前端契约由同一份词表约束。
    新增事件类型必须显式改迁移，避免前端契约在无人察觉时漂移。
    """

    RUN_STATUS = "run.status"
    MESSAGE_SNAPSHOT = "message.snapshot"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    KNOWLEDGE_PROCESSING = "knowledge.processing"
    ACTION_PROPOSED = "action.proposed"
    ACTION_SUCCEEDED = "action.succeeded"
    SOURCE_INVALIDATED = "source.invalidated"
    SCOPE_CHANGED = "scope.changed"
    ERROR = "error"
    DONE = "done"


class SummaryStatus(enum.StrEnum):
    """会话摘要状态。同会话最多一个 ``active``。"""

    ACTIVE = "active"
    STALE = "stale"


class MemoryKind(enum.StrEnum):
    """记忆类型。首版仅站长明确要求记住时创建。"""

    PREFERENCE = "preference"
    FACT = "fact"


# ----------------------------------------------------------- 操作与配额 ----
class ActionStatus(enum.StrEnum):
    """动作状态机，与 ``ActionDTO.status`` 同名同值。"""

    PROPOSED = "proposed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ActionType(enum.StrEnum):
    """动作类型。

    接口契约只给出 ``ActionDTO.type`` 这个字段，没有列全部取值；这里是
    服务端白名单的**初始集合**：发布、撤回、删除、改设置、请求补充信息，
    以及记忆的增删改与保留策略应用（``POST /api/settings/retention/preview``
    同样返回 ActionDTO）。新增类型必须显式改迁移。
    """

    PUBLISH = "publish"
    CREATE_RESOURCE = "create_resource"
    UPDATE_RESOURCE = "update_resource"
    REVOKE = "revoke"
    DELETE = "delete"
    UPDATE_SETTINGS = "update_settings"
    PROVIDE_INPUT = "provide_input"
    CREATE_MEMORY = "create_memory"
    UPDATE_MEMORY = "update_memory"
    DELETE_MEMORY = "delete_memory"
    APPLY_RETENTION = "apply_retention"


class ActionAuthorizationKind(enum.StrEnum):
    """授权来源：用户明确请求，或已确认的预览。

    模型新生成或范围不明的内容**必须**走 ``confirmed_preview``；
    ``requires_confirmation`` 由服务规则决定，模型不能把它设为 false 来直接公开。
    """

    EXPLICIT_REQUEST = "explicit_request"
    CONFIRMED_PREVIEW = "confirmed_preview"


class QuotaReservationStatus(enum.StrEnum):
    """配额预留状态机。任何转换都不倒退。"""

    RESERVED = "reserved"
    CHARGED = "charged"
    RELEASED = "released"
    REFUNDED = "refunded"


class ProviderCallStatus(enum.StrEnum):
    """外部调用状态机。``unknown`` 不能当作零费用。"""

    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ProviderCallPurpose(enum.StrEnum):
    """外部调用用途：决定成本归类与对账方式。"""

    CHAT = "chat"
    TOOL = "tool"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    SEARCH = "search"
    FETCH = "fetch"
    EMAIL = "email"


class JobStatus(enum.StrEnum):
    """任务状态，与 ``JobDTO.status`` 同名同值。

    ``waiting_auth``：需要额外验证但验证已过期，站长验证后可恢复。
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_AUTH = "waiting_auth"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class JobPhase(enum.StrEnum):
    """任务业务阶段。

    ``fetching`` / ``parsing`` / ``embedding`` 来自接口契约的 ``JobDTO.phase`` 示例；
    ``finalizing`` / ``deleting`` / ``indexing`` 是存储与索引 job 自身需要的阶段。
    """

    FETCHING = "fetching"
    PARSING = "parsing"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    FINALIZING = "finalizing"
    DELETING = "deleting"


# ------------------------------------------------------- 保留策略与审计 ----
class RetentionScope(enum.StrEnum):
    """保留策略作用域。当前默认全部 ``forever``。"""

    RESOURCES = "resources"
    CONVERSATIONS = "conversations"
    AUDIT_EVENTS = "audit_events"
    RUNTIME_LOGS = "runtime_logs"


class RetentionMode(enum.StrEnum):
    """保留模式。``forever`` 时 ``ttl_days`` 必须为空。"""

    FOREVER = "forever"
    TTL = "ttl"


class RetentionAnchor(enum.StrEnum):
    """TTL 从哪个时间点起算。"""

    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class AuditResult(enum.StrEnum):
    """审计事件的结果。``denied`` 表示权限判定拒绝了这次操作。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """枚举的**值**列表（不是成员名）。"""
    return [str(member.value) for member in enum_class]


def enum_check_expression(column: str, enum_class: type[enum.Enum]) -> str:
    """生成 ``column IN ('a', 'b')`` 形式的 CHECK 表达式。

    表达式里的字面量只来自代码里的枚举定义，不含任何外部输入。
    """
    return in_predicate(column, enum_values(enum_class))


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
    """生成 ``column IN ('a', 'b')`` 形式的部分索引谓词或 CHECK 表达式。

    谓词必须是不可变表达式：不能含子查询，也不能依赖 ``now()``。
    字面量只来自代码里的常量定义，不含任何外部输入。
    """
    items = list(values)
    if not items:
        raise ValueError("IN 谓词至少要有一个取值")
    literals = ", ".join(f"'{item}'" for item in items)
    return f"{column} IN ({literals})"
