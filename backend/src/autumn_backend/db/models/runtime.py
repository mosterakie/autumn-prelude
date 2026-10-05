"""第三批模型：对话、运行、事件、动作、配额、调用、作业与审计。

对应 ``docs/architecture/database.md`` §7、§8、§9，以及迁移批次三：
``conversations`` / ``runs`` / ``messages`` / ``actions`` / ``quota_buckets`` /
``quota_reservations`` / ``jobs`` / ``provider_calls`` / ``audit_events`` /
``run_events``。

关键结构约束：

- ``runs`` 的 ``(user_id, conversation_id)`` 与 ``(auth_session_id, user_id)``
  都用**复合外键**，保证运行、会话、授权会话属于同一用户。
- ``runs`` ↔ ``messages`` 是**循环外键**：``runs.input_message_id`` /
  ``current_message_id`` 用 ``use_alter`` + 可延迟约束在建表后补充
  （文档 §11："循环外键在相关表创建后补充，并明确可延迟检查"）。
- ``quota_reservations`` 用 ``(bucket_id, user_id)`` 与 ``(run_id, user_id)``
  复合外键，把"run 用户必须与桶用户一致"变成结构约束而不是服务约定。

**有意偏离文档**：额外增加 ``provider_calls.logical_call_key``。
文档只给 ``attempt_no``，而评审 D10 指出物理 attempt 编号不能代替逻辑调用身份；
``UNIQUE(logical_call_key, attempt_no)`` 同时覆盖 job 驱动与 run-only 两种调用。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    NON_TERMINAL_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    ActionAuthorizationKind,
    ActionStatus,
    ActionType,
    AuditResult,
    ConversationMode,
    JobPhase,
    JobStatus,
    MessageRole,
    MessageStatus,
    ProviderCallPurpose,
    ProviderCallStatus,
    QuotaReservationStatus,
    RunEventType,
    RunStatus,
    enum_check_expression,
    enum_column_type,
    in_predicate,
)
from autumn_backend.db.mixins import (
    CreatedAt,
    Deletable,
    Timestamped,
    UUIDPrimaryKey,
    Versioned,
)

#: ``event_type`` / ``jobs.kind`` 的形状：``domain.verb`` 小写点分。
_EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"


class Conversation(UUIDPrimaryKey, Timestamped, Versioned, Deletable, Base):
    """会话。

    设计要点：

    - ``mode`` 创建后不可修改；``owner`` 模式必须属于站长（service 规则）。
    - 公开资料模式的 conversation 本身**仍是私密记录**。
    - ``next_message_seq`` 是会话内消息序号的分配器。
    - ``UNIQUE(id, user_id)`` 支持 ``runs`` 的复合归属外键。
    """

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[ConversationMode] = mapped_column(
        enum_column_type(ConversationMode, length=16), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_message_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("retention_policies.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # 支持 runs(user_id, conversation_id) 的复合引用。
        UniqueConstraint("id", "user_id", name="uq_conversations_id_user_id"),
        CheckConstraint(enum_check_expression("mode", ConversationMode), name="mode_valid"),
        CheckConstraint("next_message_seq >= 1", name="next_message_seq_positive"),
        Index("ix_conversations_user_id_updated_at", "user_id", "updated_at", "id"),
        Index(
            "ix_conversations_active_user_id",
            "user_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Run(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """一次问答或助手任务。

    设计要点：

    - 幂等身份 ``UNIQUE(user_id, idempotency_key)``；语义判等用 ``request_hash``。
    - ``UNIQUE(id, conversation_id)`` 供 messages 复合引用；
      ``UNIQUE(id, user_id)`` 供 quota_reservations 复合引用。
    - Partial Unique Index 保证同一 conversation 只有一个非终态 run。
      **用户并发上限**用 users 行锁后计数，支持配置，不用固定为 1 的全局唯一索引。
    - ``next_event_seq`` 是事件序号的唯一分配器：状态变更与事件插入同事务提交。
    - ``scope_epoch`` 是建立上下文时的全站权限版本。
    - ``input_request`` 由版本化 schema 校验；锁定 run 后原子消费等待项并保存
      ``answer_hash``；相同等待项的同答案重试返回原结果，不同答案 409。
    """

    __tablename__ = "runs"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)

    input_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    status: Mapped[RunStatus] = mapped_column(
        enum_column_type(RunStatus, length=16), nullable=False, default=RunStatus.QUEUED
    )
    # 建立上下文时的全站权限版本。
    scope_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 服务端生成的框架线程标识，与 conversation + mode 绑定。
    checkpoint_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 持久事件的下一个序号；初值 1，首次分配得到 1。
    next_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # E8 执行 fencing：等待/恢复/重建上下文时递增，独立于内容 CAS 与事件序号。
    execution_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )

    # 已校验的模型与预算版本，**不含密钥**。
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # 等待补充信息：id、prompt、options、expires_at、consumed_at、answer_hash、answer_message_id。
    input_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_runs_user_id_idempotency_key"),
        # 供 messages 复合引用。
        UniqueConstraint("id", "conversation_id", name="uq_runs_id_conversation_id"),
        # 供 quota_reservations 的"同一用户"复合引用。
        UniqueConstraint("id", "user_id", name="uq_runs_id_user_id"),
        CheckConstraint("execution_generation >= 1", name="execution_generation_positive"),
        # 同一 conversation 只允许一个非终态 run；谓词由枚举派生。
        Index(
            "uq_runs_conversation_id_non_terminal",
            "conversation_id",
            unique=True,
            postgresql_where=text(in_predicate("status", NON_TERMINAL_RUN_STATUSES)),
        ),
        CheckConstraint(enum_check_expression("status", RunStatus), name="status_valid"),
        CheckConstraint("next_event_seq >= 1", name="next_event_seq_positive"),
        CheckConstraint("scope_epoch >= 0", name="scope_epoch_non_negative"),
        CheckConstraint("length(checkpoint_thread_id) > 0", name="checkpoint_thread_id_not_empty"),
        CheckConstraint("length(request_hash) > 0", name="request_hash_not_empty"),
        CheckConstraint(
            "config_snapshot IS NULL OR jsonb_typeof(config_snapshot) = 'object'",
            name="config_snapshot_is_object",
        ),
        CheckConstraint(
            "input_request IS NULL OR jsonb_typeof(input_request) = 'object'",
            name="input_request_is_object",
        ),
        # 终态必须有结束时刻，非终态必须没有。
        CheckConstraint(
            f"({in_predicate('status', TERMINAL_RUN_STATUSES)}) = (finished_at IS NOT NULL)",
            name="finished_at_matches_terminal_status",
        ),
        # runs 属于某个会话且属于同一用户。
        # 注意列顺序：本地列 (conversation_id, user_id) 对应
        # (conversations.id, conversations.user_id)。
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="fk_runs_conversation_id_user_id_conversations",
            ondelete="CASCADE",
        ),
        # 授权会话必须属于同一用户（恢复时只能换成同一用户的当前有效会话）。
        #
        # 删除动作用 ``NO ACTION`` 而不是 ``SET NULL``：SET NULL 会把列组里每一列都
        # 置空，而 ``user_id`` 是 NOT NULL，动作永远无法完成。
        # 运维语义：会话的正常生命周期是「撤销」（``revoked_at``）与过期，
        # 不是物理删除；清理作业只能删除**没有被 run 引用**的会话，
        # 否则会得到明确的约束错误而不是静默丢失归属。
        ForeignKeyConstraint(
            ["auth_session_id", "user_id"],
            ["auth_sessions.id", "auth_sessions.user_id"],
            name="fk_runs_auth_session_id_user_id",
            ondelete="NO ACTION",
        ),
        # 循环外键：messages 在 runs 之后创建，因此用 use_alter 后补，并声明可延迟。
        ForeignKeyConstraint(
            ["input_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_runs_input_message_id_messages",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["current_message_id", "conversation_id"],
            ["messages.id", "messages.conversation_id"],
            name="fk_runs_current_message_id_messages",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_runs_conversation_id_created_at", "conversation_id", "created_at"),
        Index("ix_runs_user_id_status", "user_id", "status"),
        Index("ix_runs_status_created_at", "status", "created_at"),
    )


class Message(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """消息（完整正文）。

    设计要点：

    - ``UNIQUE(conversation_id, seq)``；``seq`` 由 ``conversations.next_message_seq``
      原子分配。
    - 正文累计更新**仅限**正在生成的 assistant 消息；完成后修订通过新消息表示。
    - ``client_message_id`` 非空时按 ``(conversation_id, client_message_id)`` 去重。
    - ``run_id`` 与 ``conversation_id`` 用复合外键保证同属会话。
    - 工具协议消息保存在运行状态中，不混入普通对话展示。
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[MessageRole] = mapped_column(
        enum_column_type(MessageRole, length=16), nullable=False
    )
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    status: Mapped[MessageStatus] = mapped_column(
        enum_column_type(MessageStatus, length=16), nullable=False, default=MessageStatus.COMPLETE
    )
    client_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        # 供 runs.input_message_id / current_message_id 的复合外键引用：
        # 只有 (id, conversation_id) 唯一，才能证明"这条消息属于该会话"。
        UniqueConstraint("id", "conversation_id", name="uq_messages_id_conversation_id"),
        # 只有非空 client_message_id 参与去重。
        Index(
            "uq_messages_conversation_id_client_message_id",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        CheckConstraint(enum_check_expression("role", MessageRole), name="role_valid"),
        CheckConstraint(enum_check_expression("status", MessageStatus), name="status_valid"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("content_version >= 1", name="content_version_positive"),
        # run 与 conversation 必须同属一个会话。
        #
        # 删除动作用 ``NO ACTION``：SET NULL 会把列组里每一列都置空，
        # 而 ``conversation_id`` 是 NOT NULL。删除整个会话时消息与 run
        # 一起级联删除，提交时无悬挂引用；单独删除某个 run 必须由 service
        # 先把消息的 ``run_id`` 摘掉，否则会得到明确的约束错误。
        ForeignKeyConstraint(
            ["run_id", "conversation_id"],
            ["runs.id", "runs.conversation_id"],
            name="fk_messages_run_id_conversation_id",
            ondelete="NO ACTION",
        ),
        Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),
    )


class RunEvent(Base, CreatedAt):
    """运行事件（只追加，只有 ``created_at``）。

    设计要点：

    - 主键 ``(run_id, seq)``：序号由 ``runs.next_event_seq`` **原子分配**，
      Repository 不提供任何接受外部 seq 的写入方法。
    - ``payload`` 只含状态、对象 ID、``message_id``、``content_version`` 等必要元数据，
      **不重复保存消息全文、密钥或文件原文**。
    - 状态变更和事件插入必须同事务提交。
    """

    __tablename__ = "run_events"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[RunEventType] = mapped_column(
        enum_column_type(RunEventType, length=32), nullable=False
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("type", RunEventType), name="type_valid"),
        CheckConstraint("seq >= 0", name="seq_non_negative"),
        CheckConstraint(
            "payload IS NULL OR jsonb_typeof(payload) = 'object'", name="payload_is_object"
        ),
        Index("ix_run_events_run_id_created_at", "run_id", "created_at"),
    )


class Action(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """需要确认的持久等待动作。

    设计要点：

    - 幂等身份是不可空的 ``UNIQUE(actor_id, idempotency_key)``。
      用稳定请求身份 + ``parameters_hash`` 判等，避免可空列破坏去重语义。
    - **双版本**：``expected_version``（内容）与 ``expected_acl_version``（可见性）
      分开保存，执行时在锁内**分别**比较。行锁只能串行化动作，不能识别
      "授权已过期"：A 撤回使 ACL 从 2 变 3 后，B 仍拿着旧 ACL=2 的预览发布。
    - ``authorization_kind`` 记录授权来源；模型新生成或范围不明的内容
      **必须**走 ``confirmed_preview``。
    - 数据库内修改与 ``succeeded`` 状态同事务完成；``result`` 只存对象 ID 与状态。
    """

    __tablename__ = "actions"

    actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    # 生成这次动作的授权消息（用户明确请求的那条）。
    authorization_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )

    type: Mapped[ActionType] = mapped_column(
        enum_column_type(ActionType, length=32), nullable=False
    )
    target_resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=True
    )
    expected_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_acl_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 已校验的参数；身份不能从 parameters 读取。
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parameters_hash: Mapped[str] = mapped_column(Text, nullable=False)

    authorization_kind: Mapped[ActionAuthorizationKind] = mapped_column(
        enum_column_type(ActionAuthorizationKind, length=32),
        nullable=False,
        default=ActionAuthorizationKind.CONFIRMED_PREVIEW,
    )
    status: Mapped[ActionStatus] = mapped_column(
        enum_column_type(ActionStatus, length=32), nullable=False, default=ActionStatus.PROPOSED
    )
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("actor_id", "idempotency_key", name="uq_actions_actor_id_idempotency_key"),
        CheckConstraint(enum_check_expression("type", ActionType), name="type_valid"),
        CheckConstraint(enum_check_expression("status", ActionStatus), name="status_valid"),
        CheckConstraint(
            enum_check_expression("authorization_kind", ActionAuthorizationKind),
            name="authorization_kind_valid",
        ),
        CheckConstraint(
            "expected_version IS NULL OR expected_version >= 0",
            name="expected_version_non_negative",
        ),
        CheckConstraint(
            "expected_acl_version IS NULL OR expected_acl_version >= 0",
            name="expected_acl_version_non_negative",
        ),
        CheckConstraint(
            "parameters IS NULL OR jsonb_typeof(parameters) = 'object'", name="parameters_is_object"
        ),
        CheckConstraint("length(parameters_hash) > 0", name="parameters_hash_not_empty"),
        # 表示"用户已确认"的状态必须有确认时刻；cancelled 可在未确认时发生。
        CheckConstraint(
            "status NOT IN ('ready', 'running', 'succeeded', 'failed') OR confirmed_at IS NOT NULL",
            name="confirmed_statuses_require_confirmed_at",
        ),
        CheckConstraint(
            "status NOT IN ('proposed', 'awaiting_confirmation') OR confirmed_at IS NULL",
            name="unconfirmed_has_no_confirmed_at",
        ),
        # 只有成功执行才写 executed_at；未执行的状态不得有。
        CheckConstraint(
            "(status = 'succeeded') = (executed_at IS NOT NULL)",
            name="executed_at_matches_status",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_actions_run_id_status", "run_id", "status"),
        Index("ix_actions_status_expires_at", "status", "expires_at"),
        Index("ix_actions_target_resource_id", "target_resource_id"),
    )


class QuotaBucket(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """每日额度桶。

    设计要点：

    - 窗口按 ``Asia/Shanghai`` 00:00 切分；``window_start`` / ``window_end`` 存 UTC，
      ``timezone`` 记录切分依据。
    - **不保存静态限额**：限额从当前受控配置读取，否则历史桶里的静态约束会让
      站长无法降低额度。受理时同事务加锁并比较 ``used + reserved`` 与当前限额。
    - 并发纪律：首次创建必须 ``INSERT ... ON CONFLICT (user_id, window_start) DO NOTHING``
      **再** ``SELECT ... FOR UPDATE``——不能指望锁住不存在的行。
    """

    __tablename__ = "quota_buckets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Shanghai")
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 受理时使用的策略版本，便于解释历史扣次。
    policy_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "window_start", name="uq_quota_buckets_user_window"),
        # 供 quota_reservations 的"桶与用户一致"复合引用。
        UniqueConstraint("id", "user_id", name="uq_quota_buckets_id_user_id"),
        CheckConstraint("used >= 0", name="used_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("policy_version >= 0", name="policy_version_non_negative"),
        CheckConstraint("window_end > window_start", name="window_ordered"),
        Index("ix_quota_buckets_window_start", "window_start"),
    )


class QuotaReservation(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """配额预留。

    状态转换与桶计数**同事务**进行（阶段 B5）：

    - ``reserved → charged``：``reserved-1``、``used+1``
    - ``reserved → released``：``reserved-1``
    - ``charged → refunded``：``used-1``

    禁止重复补偿；``CHECK(amount = 1)``；run 用户与桶用户一致由复合外键保证。
    计数桶不保存费用；午夜后重连仍使用原 reservation。
    """

    __tablename__ = "quota_reservations"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bucket_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[QuotaReservationStatus] = mapped_column(
        enum_column_type(QuotaReservationStatus, length=16),
        nullable=False,
        default=QuotaReservationStatus.RESERVED,
    )
    charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_quota_reservations_run_id"),
        CheckConstraint(
            enum_check_expression("status", QuotaReservationStatus), name="status_valid"
        ),
        # 首版额度单位固定为 1。
        CheckConstraint("amount = 1", name="amount_is_one"),
        # reserved 尚未进入供应商阶段。
        CheckConstraint("status <> 'reserved' OR charged_at IS NULL", name="reserved_not_charged"),
        # 终态（released / refunded）必须有结算时刻。
        CheckConstraint(
            "status NOT IN ('released', 'refunded') OR settled_at IS NOT NULL",
            name="settled_at_matches_terminal_status",
        ),
        # run 必须与本预留属于同一用户。
        ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["runs.id", "runs.user_id"],
            name="fk_quota_reservations_run_id_user_id",
            ondelete="CASCADE",
        ),
        # 桶必须与本预留属于同一用户（取代文档建议的"约束触发器校验"）。
        ForeignKeyConstraint(
            ["bucket_id", "user_id"],
            ["quota_buckets.id", "quota_buckets.user_id"],
            name="fk_quota_reservations_bucket_id_user_id",
            ondelete="CASCADE",
        ),
        Index("ix_quota_reservations_bucket_id_status", "bucket_id", "status"),
        Index("ix_quota_reservations_user_id_created_at", "user_id", "created_at"),
    )


class ProviderCall(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """外部调用记录（受限状态机，不是 AppendOnly）。

    设计要点：

    - ``prepared → dispatched → succeeded | failed | unknown``；
      ``settle_*`` 必须 ``WHERE status='dispatched'``，0 行即 ``ConflictError``。
    - ``unknown`` 不能当作零费用。
    - **稳定逻辑身份与物理尝试分开**（评审 D10）：``logical_call_key`` 是稳定身份，
      ``attempt_no`` 是物理尝试编号。``UNIQUE(logical_call_key, attempt_no)``
      锁定"同一逻辑调用的某一次尝试"；重送时**复用**外部幂等键，
      不会因为 attempt 增加而换键导致重复执行。
    - 不同币种不能直接相加；真实密钥、鉴权头与完整提示词**不写入本表**。
    """

    __tablename__ = "provider_calls"

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[ProviderCallPurpose] = mapped_column(
        enum_column_type(ProviderCallPurpose, length=16), nullable=False
    )
    # 稳定逻辑身份：同一笔业务操作无论重试多少次都用同一个值。
    logical_call_key: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    status: Mapped[ProviderCallStatus] = mapped_column(
        enum_column_type(ProviderCallStatus, length=16),
        nullable=False,
        default=ProviderCallStatus.PREPARED,
    )
    # provider 侧返回的调用 ID，用于对账 unknown。
    external_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    search_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "logical_call_key", "attempt_no", name="uq_provider_calls_logical_key_attempt"
        ),
        CheckConstraint(enum_check_expression("status", ProviderCallStatus), name="status_valid"),
        CheckConstraint(
            enum_check_expression("purpose", ProviderCallPurpose), name="purpose_valid"
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint("length(logical_call_key) > 0", name="logical_call_key_not_empty"),
        # 调用必须能追溯到 run 或 job。
        CheckConstraint("run_id IS NOT NULL OR job_id IS NOT NULL", name="traceable_target"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_non_negative"
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_non_negative"
        ),
        CheckConstraint(
            "search_units IS NULL OR search_units >= 0", name="search_units_non_negative"
        ),
        CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0", name="estimated_cost_non_negative"
        ),
        CheckConstraint("actual_cost IS NULL OR actual_cost >= 0", name="actual_cost_non_negative"),
        # 有费用就必须有币种：不同币种不能直接相加。
        CheckConstraint(
            "(estimated_cost IS NULL AND actual_cost IS NULL) OR currency IS NOT NULL",
            name="currency_required_with_cost",
        ),
        CheckConstraint("currency IS NULL OR length(currency) = 3", name="currency_shape"),
        # prepared 尚未派出。
        CheckConstraint("status <> 'prepared' OR started_at IS NULL", name="prepared_not_started"),
        # 终态必须有结束时刻。
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'unknown') OR finished_at IS NOT NULL",
            name="finished_at_matches_terminal_status",
        ),
        Index("ix_provider_calls_provider_created_at", "provider", "created_at"),
        Index("ix_provider_calls_run_id", "run_id"),
        Index("ix_provider_calls_job_id", "job_id"),
        Index("ix_provider_calls_status_created_at", "status", "created_at"),
    )


class Job(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """持久作业（PostgreSQL lease queue）。

    设计要点：

    - ``kind`` 是 ``domain.verb`` 形状的业务类型（如 ``run.dispatch``）。
    - ``idempotency_key`` 唯一：防止重复排队。
    - 领取用 ``FOR UPDATE SKIP LOCKED``；结果更新必须匹配 ``lease_token``，
      防止过期 worker 覆盖新结果。
    - 需要额外验证且验证过期的任务转 ``waiting_auth``，站长验证后可恢复。
    - ``progress`` 不确定时为 NULL，**不编造百分比**。
    """

    __tablename__ = "jobs"

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column_type(JobStatus, length=16), nullable=False, default=JobStatus.QUEUED
    )
    phase: Mapped[JobPhase | None] = mapped_column(
        enum_column_type(JobPhase, length=16), nullable=True
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=True
    )

    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        CheckConstraint(enum_check_expression("status", JobStatus), name="status_valid"),
        CheckConstraint(enum_check_expression("phase", JobPhase), name="phase_valid"),
        CheckConstraint("length(kind) > 0", name="kind_not_empty"),
        CheckConstraint(f"kind ~ '{_EVENT_TYPE_PATTERN}'", name="kind_shape"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key_not_empty"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 100)", name="progress_in_range"
        ),
        CheckConstraint(
            "payload IS NULL OR jsonb_typeof(payload) = 'object'", name="payload_is_object"
        ),
        CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'", name="result_is_object"
        ),
        # running 必须同时具备 lease_token 与 lease_expires_at；其余状态不得残留。
        CheckConstraint(
            "(status = 'running') = (lease_token IS NOT NULL) "
            "AND (status = 'running') = (lease_expires_at IS NOT NULL)",
            name="lease_fields_match_status",
        ),
        # 领取索引：(status, available_at)。
        Index("ix_jobs_status_available_at", "status", "available_at"),
        Index("ix_jobs_lease_expires_at", "lease_expires_at"),
        Index("ix_jobs_run_id", "run_id"),
        Index("ix_jobs_resource_id", "resource_id"),
    )


class AuditEvent(UUIDPrimaryKey, CreatedAt, Base):
    """审计事件（只追加，只有 ``created_at``）。

    设计要点：

    - Repository 只暴露 ``record()``，不提供通用 update/delete。
    - ``before_version`` / ``after_version`` **专指** ``resources.version``；
      可见性等其它前后值写入 ``metadata``。
    - ``metadata`` 不存密码、token、完整私人文档或模型上下文；
      主要保留对象 ID、字段名、状态和版本。
    - 默认永久（``expires_at`` 为空），主动清理按明确策略进行。
    - ``event_type`` 用 ``domain.verb`` 形状约束而不冻结取值：审计类型会演进。
    """

    __tablename__ = "audit_events"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("actions.id", ondelete="SET NULL"), nullable=True
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[AuditResult] = mapped_column(
        enum_column_type(AuditResult, length=16), nullable=False
    )
    before_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("result", AuditResult), name="result_valid"),
        CheckConstraint(f"event_type ~ '{_EVENT_TYPE_PATTERN}'", name="event_type_shape"),
        CheckConstraint(
            "metadata_json IS NULL OR jsonb_typeof(metadata_json) = 'object'",
            name="metadata_is_object",
        ),
        CheckConstraint(
            "before_version IS NULL OR before_version >= 0", name="before_version_non_negative"
        ),
        CheckConstraint(
            "after_version IS NULL OR after_version >= 0", name="after_version_non_negative"
        ),
        Index("ix_audit_events_resource_id_created_at", "resource_id", "created_at"),
        Index("ix_audit_events_actor_id_created_at", "actor_id", "created_at"),
        Index("ix_audit_events_event_type_created_at", "event_type", "created_at"),
        Index("ix_audit_events_action_id", "action_id"),
    )


__all__ = [
    "Action",
    "AuditEvent",
    "Conversation",
    "Job",
    "Message",
    "ProviderCall",
    "QuotaBucket",
    "QuotaReservation",
    "Run",
    "RunEvent",
]
