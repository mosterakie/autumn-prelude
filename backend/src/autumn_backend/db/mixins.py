"""可复用 Mixin：主键、乐观锁版本、时间戳、软删除。

设计取舍（对齐 ``docs/architecture/database.md`` §1）：

- **业务 ID 用 UUID 且由应用生成**：``default=uuid.uuid4`` 是主路径。
  同时保留 ``server_default=gen_random_uuid()`` 作为兜底，让原生 SQL 与
  Repository 的 UPSERT 不必自己生成主键（这是对文档的**有意偏离**，见 README）。
- **可变对象用 ``bigint version``**：文档明确要求 bigint 而非 integer。
- **时间统一 timestamptz（UTC）**：连接层强制 ``timezone=UTC``，
  ``onupdate`` 与数据库触发器都写 ``now()``。
- **``Versioned`` 只服务内容/元数据的乐观并发**；可见性版本 ``acl_version``
  与全站 ``scope_epoch`` 不是行级 CAS 版本，因此**不放进 Mixin**，
  由需要的表显式声明。
- **只追加表只声明 ``created_at``**：``run_events`` / ``audit_events`` 没有
  ``updated_at``，因此也不会挂时间戳触发器。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Uuid, event, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Table

from autumn_backend.db.base import Base
from autumn_backend.db.timestamps import updated_at_trigger_statements

__all__ = [
    "CreatedAt",
    "Deletable",
    "SoftDelete",
    "SoftDeleteState",
    "Timestamped",
    "UUIDPrimaryKey",
    "Versioned",
    "timestamped_tables",
]


class SoftDeleteState(enum.StrEnum):
    """软删除的两个时刻组合出的三种互斥状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class UUIDPrimaryKey:
    """UUID 主键，由应用生成。

    只有真正使用 UUID 主键的表才继承它。复合主键与 str 主键表
    （``settings``、``rate_limit_buckets``）不套用。
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class Versioned:
    """乐观并发版本列（``bigint``）。

    语义边界（架构文档 §5）：``version`` 只在**私人内容或 metadata 变化**时递增。
    publish / revoke / 软删除 / 恢复只递增 ``acl_version``，**不**递增 ``version``。

    CAS 由 Repository 单语句完成::

        UPDATE ... SET version = :expected + 1, ...
        WHERE id = :id AND version = :expected
        RETURNING ...            -- 0 行 -> OptimisticLockError
    """

    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    def version_matches(self, expected_version: int) -> bool:
        """当前内存对象的版本是否等于调用方持有的版本。"""
        return int(self.version) == int(expected_version)


class CreatedAt:
    """只有 ``created_at``：用于只追加表（run_events / audit_events）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Timestamped(CreatedAt):
    """``created_at`` + ``updated_at``，均为 UTC timestamptz。

    ``updated_at`` 双重保障：ORM 的 ``onupdate`` + 数据库 BEFORE UPDATE 触发器
    （见 ``db/timestamps.py``）。触发器在 ``Table.after_create`` 时挂载，
    迁移脚本另需显式挂一次——:func:`timestamped_tables` 给迁移提供表清单。
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Deletable:
    """只有 ``deleted_at``：账号、会话、留言、记忆等"主动删除入口"。"""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class SoftDelete(Deletable):
    """``deleted_at`` + ``archived_at``：资源的归档与删除是两个独立时刻。"""

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None and self.archived_at is None

    @property
    def soft_delete_state(self) -> SoftDeleteState:
        """两个时刻组合出的状态；删除优先于归档。"""
        if self.deleted_at is not None:
            return SoftDeleteState.DELETED
        if self.archived_at is not None:
            return SoftDeleteState.ARCHIVED
        return SoftDeleteState.ACTIVE


@event.listens_for(Table, "after_create")
def _attach_updated_at_trigger(target: Table, connection: Connection, **_kwargs: object) -> None:
    """任何带 ``updated_at`` 的表在创建后自动挂上时间戳触发器。

    SQLAlchemy 模型元数据里没有触发器概念，因此这是唯一的声明式挂载点。
    Alembic 迁移建表时不会触发该事件，迁移脚本需自行调用
    ``updated_at_trigger_statements``。
    """
    if "updated_at" not in target.columns:
        return
    # 逐条执行：asyncpg 不接受一次预编译多条命令。
    for statement in updated_at_trigger_statements(target):
        connection.exec_driver_sql(statement)


def timestamped_tables() -> tuple[Table, ...]:
    """当前元数据中所有带 ``updated_at`` 的表。

    迁移脚本用它批量生成 ``CREATE TRIGGER``，避免手写表清单漏掉新表。
    """
    return tuple(table for table in Base.metadata.sorted_tables if "updated_at" in table.columns)
