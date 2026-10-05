"""第三批模型：Run、配额、Job、调用记录、审计与动作。

覆盖实施顺序 A6 的十张表：

- :class:`Conversation` / :class:`Message` —— 会话与消息正文
- :class:`Run` —— 一次 ask；幂等身份 ``(user_id, idempotency_key)``
- :class:`RunEvent` —— 只追加的事件流，``(run_id, seq)`` 主键
- :class:`QuotaBucket` / :class:`QuotaReservation` —— 每日额度与预留状态机
- :class:`Job` —— 持久队列（SKIP LOCKED + lease）
- :class:`ProviderCall` —— 外部调用受限状态机
- :class:`AuditEvent` —— 真正只追加的审计
- :class:`Action` —— 需要用户确认的持久等待

本批是约束最密集的一批，实施顺序表要求的每一条都在下面落成命名约束：

- ``runs`` ``UNIQUE(user_id, idempotency_key)``
- ``runs`` Partial Unique Index：同 conversation 的非终态 run 唯一
- ``runs.next_event_seq``：``run_events.emit()`` 靠它原子分配 seq
- ``quota_buckets`` ``UNIQUE(user_id, window_start)`` + ``CHECK(used >= 0)`` +
  ``CHECK(reserved >= 0)``
- ``quota_reservations`` ``UNIQUE(run_id)`` + ``CHECK(amount = 1)``
- ``provider_calls`` 状态 ``CHECK`` + ``UNIQUE(job_id, purpose, attempt_no)``
- ``jobs`` 状态 / lease 字段 ``CHECK``
- ``run_events`` ``PRIMARY KEY(run_id, seq)``（即 ``UNIQUE(run_id, seq)``）

另外给 A5 留下的 ``run_sources.run_id`` 补上指向 ``runs`` 的外键。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    NON_TERMINAL_RUN_STATUSES,
    ActionKind,
    ActionStatus,
    ConversationMode,
    EventType,
    JobStatus,
    MessageRole,
    ModelProfile,
    ProviderCallPurpose,
    ProviderCallStatus,
    QuotaReservationStatus,
    RunStatus,
    enum_check_expression,
    enum_column_type,
    in_predicate,
)
from autumn_backend.db.mixins import Timestamped, UUIDPrimaryKey, Versioned

_HASH_LENGTH = 64
_IDEMPOTENCY_KEY_LENGTH = 128
_JOB_TYPE_LENGTH = 64
_PROVIDER_LENGTH = 64


class Conversation(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """会话。

    设计要点：

    - 一个 conversation 固定一种 ``mode``（``public`` 或 ``owner``）；
      ``mode`` 参与 Partial Unique Index 之外的权限判定，也参与 ``thread_id`` 派生。
    - ``thread_id`` **仅由服务端生成**并与 conversation + mode 绑定，
      因此它唯一且不可由客户端指定。
    - ``content_acl_epoch_at_start`` 记录会话建立时的全站 epoch；
      epoch 变化会让旧 generation 失效（架构文档 §6.2）。
    """

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[ConversationMode] = mapped_column(
        enum_column_type(ConversationMode, length=16), nullable=False
    )
    # 服务端生成的线程标识：绑定 conversation + mode，客户端无法指定。
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    model_profile: Mapped[ModelProfile] = mapped_column(
        enum_column_type(ModelProfile, length=16), nullable=False, default=ModelProfile.PRIMARY
    )

    # 会话建立时的全站公开权限 epoch；变化即意味着上下文可能失效。
    content_acl_epoch_at_start: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_conversations_thread_id"),
        CheckConstraint(enum_check_expression("mode", ConversationMode), name="mode_valid"),
        CheckConstraint(
            enum_check_expression("model_profile", ModelProfile), name="model_profile_valid"
        ),
        CheckConstraint("content_acl_epoch_at_start >= 0", name="acl_epoch_at_start_non_negative"),
        Index("ix_conversations_user_id_created_at", "user_id", "created_at"),
        Index("ix_conversations_user_id_last_activity_at", "user_id", "last_activity_at"),
    )


class Message(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """完整消息文本。

    与 ``run_events`` 的分工（架构文档 §9.3）：**消息正文存这里**，
    ``run_events`` 只存事件、对象 ID、版本与必要元数据。
    ``content_version`` 供 SSE 累计快照的前端按版本替换。
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # 触发这条消息的 run；system 消息可以为 NULL。
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    # 发送者；system 消息为 NULL。
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    role: Mapped[MessageRole] = mapped_column(
        enum_column_type(MessageRole, length=16), nullable=False
    )
    # 同一会话内的单调序号，用于稳定排序与分页。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 内容结构版本（与 ``users.content_schema_version`` 对应）。
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # 工具消息才有：工具名与结构化结果。
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
        CheckConstraint(enum_check_expression("role", MessageRole), name="role_valid"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("content_version >= 1", name="content_version_positive"),
        # 工具消息必须带工具名；非工具消息不应带工具名。
        CheckConstraint(
            "(role = 'tool') = (tool_name IS NOT NULL)",
            name="tool_name_matches_role",
        ),
        Index("ix_messages_conversation_id_seq", "conversation_id", "seq"),
    )


class Run(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """一次 ask。

    设计要点：

    - 幂等身份 ``(user_id, idempotency_key)``；语义判等用 ``request_hash``。
      同一 key 不同 hash 必须映射 409 ``IDEMPOTENCY_CONFLICT``。
    - ``next_event_seq`` 是事件序号的**唯一**分配器：
      ``UPDATE runs SET next_event_seq = next_event_seq + 1 RETURNING next_event_seq - 1``。
      禁止外部指定 seq——否则并发下会重号。
    - Partial Unique Index 保证同一 conversation 只有一个非终态 run。
    - ``quota_bucket_id`` 在受理时固定，跨午夜恢复**不换桶、不重扣**。
    - 终止原因用 ``error_code`` / ``terminal_reason`` 表达，不扩张 ``status``。
    """

    __tablename__ = "runs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # 受理时固定的额度桶：跨午夜恢复不换桶。
    quota_bucket_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("quota_buckets.id", ondelete="SET NULL"), nullable=True
    )

    idempotency_key: Mapped[str] = mapped_column(String(_IDEMPOTENCY_KEY_LENGTH), nullable=False)
    # 同一幂等键的语义判等：稳定 JSON 序列化后的 SHA-256。
    request_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)

    status: Mapped[RunStatus] = mapped_column(
        enum_column_type(RunStatus, length=16), nullable=False, default=RunStatus.PENDING
    )
    mode: Mapped[ConversationMode] = mapped_column(
        enum_column_type(ConversationMode, length=16), nullable=False
    )
    model_profile: Mapped[ModelProfile] = mapped_column(
        enum_column_type(ModelProfile, length=16), nullable=False, default=ModelProfile.PRIMARY
    )

    # 事件序号分配器：只有一个写入者能拿到某个 seq。
    # 初值 1 表示序号从 1 开始；分配语句是
    #   UPDATE runs SET next_event_seq = next_event_seq + 1 RETURNING next_event_seq - 1
    # 因此第一次分配得到 1。
    next_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 上下文重建代数：ACL 变化后重新构建上下文会递增。
    context_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 冻结的 ACL 上下文：全站 epoch 与资源可见性版本的快照。
    acl_epoch_at_start: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # 会话与步骤升级都是受理时的快照；恢复时必须服务端重新鉴权，不信任 checkpoint。
    auth_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="SET NULL"), nullable=True
    )
    step_up_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 预算与用量：受预算限制的"完成或继续"。
    steps_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # 终止语义：主状态保持有限集合，原因放这里。
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 幂等身份：同 key 同 hash 返回原 run；不同 hash 冲突。
        UniqueConstraint("user_id", "idempotency_key", name="uq_runs_user_id_idempotency_key"),
        # 同一 conversation 只允许一个非终态 run。
        # 谓词取自 NON_TERMINAL_RUN_STATUSES，索引与枚举不可能各自漂移。
        Index(
            "uq_runs_conversation_id_non_terminal",
            "conversation_id",
            unique=True,
            postgresql_where=text(in_predicate("status", NON_TERMINAL_RUN_STATUSES)),
        ),
        CheckConstraint(enum_check_expression("status", RunStatus), name="status_valid"),
        CheckConstraint(enum_check_expression("mode", ConversationMode), name="mode_valid"),
        CheckConstraint(
            enum_check_expression("model_profile", ModelProfile), name="model_profile_valid"
        ),
        CheckConstraint("next_event_seq >= 1", name="next_event_seq_positive"),
        CheckConstraint("context_generation >= 1", name="context_generation_positive"),
        CheckConstraint("acl_epoch_at_start >= 0", name="acl_epoch_at_start_non_negative"),
        CheckConstraint("steps_used >= 0", name="steps_used_non_negative"),
        CheckConstraint("token_used >= 0", name="token_used_non_negative"),
        CheckConstraint("cost_micro_usd >= 0", name="cost_micro_usd_non_negative"),
        # 终态必须有结束时刻，非终态必须没有：避免"已完成但没有完成时间"。
        CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="finished_at_matches_terminal_status",
        ),
        Index("ix_runs_conversation_id_created_at", "conversation_id", "created_at"),
        Index("ix_runs_user_id_status", "user_id", "status"),
        Index("ix_runs_status_created_at", "status", "created_at"),
    )


class RunEvent(Timestamped, Base):
    """只追加的事件流。

    设计要点：

    - 主键 ``(run_id, seq)`` 同时就是实施顺序要求的 ``UNIQUE(run_id, seq)``。
    - ``seq`` **只能**由 ``Run.next_event_seq`` 的原子自增分配，
      Repository 不提供任何接受外部 seq 的写入方法。
    - 只存事件、对象 ID、版本与必要元数据；完整消息正文在 ``messages``。
    """

    __tablename__ = "run_events"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    # 由 runs.next_event_seq 原子分配，不可由调用方指定。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    event_type: Mapped[EventType] = mapped_column(
        enum_column_type(EventType, length=32), nullable=False
    )
    # 事件引用的对象（消息 / 动作 / 来源片段）；无引用时为 NULL。
    object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    object_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 事件发生时的 ACL 上下文代数，便于判定"这条输出是否已失效"。
    context_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)

    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("run_id", "seq", name="pk_run_events"),
        CheckConstraint(enum_check_expression("event_type", EventType), name="event_type_valid"),
        CheckConstraint("seq >= 0", name="seq_non_negative"),
        CheckConstraint(
            "object_version IS NULL OR object_version >= 0", name="object_version_non_negative"
        ),
        CheckConstraint(
            "context_generation IS NULL OR context_generation >= 1",
            name="context_generation_positive",
        ),
        Index("ix_run_events_run_id_created_at", "run_id", "created_at"),
    )


class QuotaBucket(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """每日额度桶。

    时间语义（架构文档 §7.3）：窗口按 ``Asia/Shanghai`` 00:00 切分，
    但 ``window_start`` 以 UTC 存储；``window_timezone`` 记录切分依据，
    避免"改了配置就看不懂历史桶"。

    并发纪律：首次创建必须 ``INSERT ... ON CONFLICT (user_id, window_start)
    DO NOTHING`` **再** ``SELECT ... FOR UPDATE``——不能指望锁住不存在的行。
    """

    __tablename__ = "quota_buckets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # UTC 时刻；等于该时区当天的 00:00。
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 切分依据的时区名，默认 Asia/Shanghai。
    window_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Shanghai"
    )

    limit_value: Mapped[int] = mapped_column(Integer, nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "window_start", name="uq_quota_buckets_user_window"),
        CheckConstraint("used >= 0", name="used_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("limit_value >= 0", name="limit_value_non_negative"),
        # 已用 + 已预留不得超过上限：这是"并发受理不突破日额度"的数据库兜底。
        CheckConstraint("used + reserved <= limit_value", name="usage_within_limit"),
        Index("ix_quota_buckets_window_start", "window_start"),
    )


class QuotaReservation(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """配额预留。

    设计要点：

    - 幂等键就是 ``run_id``（``UNIQUE(run_id)``）；首版额度单位固定为 1
      （``CHECK(amount = 1)``），因此不需要"预留明细"子表。
    - 只有**首次**成功 INSERT 才增加 ``bucket.reserved``；
      冲突分支回读并校验 ``bucket_id`` / ``amount``，不一致即 ``ConflictError``。
    - 状态机不倒退：``charged`` 不退回 ``reserved``，``refunded`` 不二次改 ``used``。
    - 超额必须抛异常让整个 UoW 回滚，连带撤销刚插入的 reservation。
    """

    __tablename__ = "quota_reservations"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("quota_buckets.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[QuotaReservationStatus] = mapped_column(
        enum_column_type(QuotaReservationStatus, length=16),
        nullable=False,
        default=QuotaReservationStatus.RESERVED,
    )

    charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 结算时的原因码：供应商故障、本地取消等。
    settle_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_quota_reservations_run_id"),
        CheckConstraint(
            enum_check_expression("status", QuotaReservationStatus), name="status_valid"
        ),
        # 首版额度单位固定为 1：简化并发仲裁，也让 CHECK 能兜住"偷偷改单位"。
        CheckConstraint("amount = 1", name="amount_is_one"),
        CheckConstraint("status <> 'reserved' OR charged_at IS NULL", name="reserved_not_charged"),
        Index("ix_quota_reservations_bucket_id_status", "bucket_id", "status"),
        Index("ix_quota_reservations_user_id_created_at", "user_id", "created_at"),
    )


class Job(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """持久队列任务（PostgreSQL lease queue）。

    设计要点：

    - ``claim`` 使用 ``FOR UPDATE SKIP LOCKED``；``heartbeat`` / ``finish``
      **必须匹配** ``lease_token`` 且 ``status='running'``，0 行即表示 lease 已丢失。
    - ``lease_token`` 控制的是"是否仍有资格提交结果"，**不保证**已发出的外部请求被撤销。
    - ``run_id`` 让 worker 能从服务端记录重建 ActorContext，而不是携带浏览器 Cookie。
    - ``dedupe_key`` 幂等：同一业务对象在活跃状态下只允许一个同类任务。
    """

    __tablename__ = "jobs"

    job_type: Mapped[str] = mapped_column(String(_JOB_TYPE_LENGTH), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column_type(JobStatus, length=16), nullable=False, default=JobStatus.QUEUED
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    # 业务幂等键：同一对象在活跃状态下只应有一个同类任务。
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("status", JobStatus), name="status_valid"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        # running 必须同时具备 lease_token 与 lease_expires_at；其余状态不得残留 lease。
        CheckConstraint(
            "(status = 'running') = (lease_token IS NOT NULL) "
            "AND (status = 'running') = (lease_expires_at IS NOT NULL)",
            name="lease_fields_match_status",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="finished_at_matches_terminal_status",
        ),
        # 幂等：同一业务对象在活跃状态下只允许一个同类任务。
        # 终态行不受约束，因此重试可以新建任务。
        Index(
            "uq_jobs_type_dedupe_active",
            "job_type",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
        # claim 的取任务顺序：available_at, id。
        Index("ix_jobs_status_available_at", "status", "available_at"),
        Index("ix_jobs_lease_expires_at", "lease_expires_at"),
        Index("ix_jobs_run_id", "run_id"),
    )


class ProviderCall(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """外部调用记录（受限状态机，不是 AppendOnly）。

    设计要点：

    - ``prepared -> dispatched -> succeeded | failed | unknown``；
      ``settle_*`` 必须 ``WHERE status='dispatched'``，0 行即 ``ConflictError``。
    - ``unknown`` 用于网络超时或无法确认远端状态，**不能**当作"失败且零成本"。
    - ``UNIQUE(job_id, purpose, attempt_no)`` 锁定 attempt 身份，
      同时可作为外部幂等键（``job_id + purpose + attempt_no``）。
    - ``idempotency_key`` 在 provider 支持幂等时透传。
    """

    __tablename__ = "provider_calls"

    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )

    provider: Mapped[str] = mapped_column(String(_PROVIDER_LENGTH), nullable=False)
    purpose: Mapped[ProviderCallPurpose] = mapped_column(
        enum_column_type(ProviderCallPurpose, length=16), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[ProviderCallStatus] = mapped_column(
        enum_column_type(ProviderCallStatus, length=16),
        nullable=False,
        default=ProviderCallStatus.PREPARED,
    )
    # provider 支持幂等时透传的键；同时用于对账。
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # provider 侧返回的调用 ID，用于对账 unknown。
    provider_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_micro_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 失败/未知时的错误码与对账状态。
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # attempt 身份：同一 job 的同一用途的同一 attempt 只能有一条记录。
        UniqueConstraint(
            "job_id", "purpose", "attempt_no", name="uq_provider_calls_job_purpose_attempt"
        ),
        CheckConstraint(enum_check_expression("status", ProviderCallStatus), name="status_valid"),
        CheckConstraint(
            enum_check_expression("purpose", ProviderCallPurpose), name="purpose_valid"
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_non_negative"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_non_negative"
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_non_negative"
        ),
        CheckConstraint("cost_micro_usd IS NULL OR cost_micro_usd >= 0", name="cost_non_negative"),
        # 调用必须能追溯到 job 或 run，否则无法对账也无法重建身份。
        CheckConstraint("job_id IS NOT NULL OR run_id IS NOT NULL", name="traceable_target"),
        # prepared 尚未派出；dispatched 之后才允许有派发时刻。
        CheckConstraint(
            "status <> 'prepared' OR dispatched_at IS NULL", name="prepared_not_dispatched"
        ),
        # 未结算的状态不得有 settled_at。
        CheckConstraint(
            "status IN ('succeeded', 'failed', 'unknown') = (settled_at IS NOT NULL)",
            name="settled_at_matches_status",
        ),
        Index("ix_provider_calls_status_created_at", "status", "created_at"),
        Index("ix_provider_calls_run_id", "run_id"),
        Index("ix_provider_calls_provider_call_id", "provider_call_id"),
    )


class AuditEvent(UUIDPrimaryKey, Timestamped, Base):
    """审计事件（真正只追加）。

    设计要点：

    - Repository 只暴露 ``record()``，不提供通用 update/delete。
    - ``before_version`` / ``after_version`` **专指** ``resources.version``；
      可见性（ACL）的前后值写入 ``metadata_json``，不占用版本字段
      （Repository 文档 §11）。
    - ``actor_id`` 允许为空：系统动作与已删除用户的动作都要保留审计事实。
      ``SET NULL`` 保证删账号不会连审计一起删掉。
    - **审计不等于日志**：这里的记录是业务事实，不参与日志轮转与脱敏管道。
    """

    __tablename__ = "audit_events"

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 有权访问的会话（请求来源），随会话删除而置空。
    actor_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="SET NULL"), nullable=True
    )
    # 关系型引用：删除目标时置空，但审计事实保留。
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="SET NULL"), nullable=True
    )
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )

    # 专指 resources.version，不表示 acl_version。
    before_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ACL 前后值、目标类型与摘要等放进 metadata。
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint("length(action) > 0", name="action_not_empty"),
        CheckConstraint(
            "before_version IS NULL OR before_version >= 0", name="before_version_non_negative"
        ),
        CheckConstraint(
            "after_version IS NULL OR after_version >= 0", name="after_version_non_negative"
        ),
        Index("ix_audit_events_resource_id_created_at", "resource_id", "created_at"),
        Index("ix_audit_events_actor_id_created_at", "actor_id", "created_at"),
        Index("ix_audit_events_action_created_at", "action", "created_at"),
        Index("ix_audit_events_run_id", "run_id"),
    )


class Action(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """需要用户确认的持久等待。

    设计要点：

    - 幂等身份：``(run_id, kind, target_id, target_version, args_hash)``。
      重复请求返回原 action，而不是新建第二条等待。
    - ``args_hash`` 是参数摘要的 SHA-256：**目标、版本、参数**共同决定语义。
    - ``target_version`` 绑定目标当时的版本：目标已变化则确认必须被拒绝，
      而不是静默地作用在"新版本"上。
    - ``expires_at`` 到点即 ``expired``；过期后确认必须失败。
    """

    __tablename__ = "actions"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[ActionKind] = mapped_column(
        enum_column_type(ActionKind, length=32), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    # 目标当时的版本或 acl_version；目标变化后确认必须被拒绝。
    target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    args_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    args_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)

    status: Mapped[ActionStatus] = mapped_column(
        enum_column_type(ActionStatus, length=16), nullable=False, default=ActionStatus.PENDING
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 每个非 pending 状态各自的转换时刻；与状态**双向**一致，防止"半截转换"。
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        # 幂等身份：同一 run 的同一动作语义只应存在一条。
        UniqueConstraint(
            "run_id",
            "kind",
            "target_id",
            "target_version",
            "args_hash",
            name="uq_actions_idempotency_identity",
        ),
        CheckConstraint(enum_check_expression("kind", ActionKind), name="kind_valid"),
        CheckConstraint(enum_check_expression("status", ActionStatus), name="status_valid"),
        CheckConstraint(
            "target_version IS NULL OR target_version >= 0", name="target_version_non_negative"
        ),
        # 状态与转换时刻一致性。用**包含式**约束而不是双向等式：
        # 双向等式无法表达"先确认、后执行"（执行态同时需要 confirmed_at）。
        # 包含式同样能拦住"半截转换"：pending 状态带上任何转换时刻都会被拒绝。
        CheckConstraint(
            "(confirmed_at IS NULL) = (status NOT IN ('confirmed', 'executed'))",
            name="confirmed_at_matches_status",
        ),
        CheckConstraint(
            "(executed_at IS NULL) = (status <> 'executed')",
            name="executed_at_matches_status",
        ),
        CheckConstraint(
            "(rejected_at IS NULL) = (status <> 'rejected')",
            name="rejected_at_matches_status",
        ),
        CheckConstraint(
            "(expired_at IS NULL) = (status <> 'expired')",
            name="expired_at_matches_status",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_actions_run_id_status", "run_id", "status"),
        Index("ix_actions_status_expires_at", "status", "expires_at"),
    )
