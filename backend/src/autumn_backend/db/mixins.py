"""可复用 Mixin：主键、乐观锁版本、时间戳、软删除。

设计取舍（对齐 Repository/UoW 文档 §1、§3 与架构文档 §5）：

- **``Versioned.version`` 只服务``resources.version`` 语义**（私人内容/metadata 的
  乐观并发）。可见性版本 ``acl_version`` 与全站 ``content_acl_epoch`` **不放进
  这个 Mixin**：它们不是行级 CAS 版本，混在一起会让 publish/revoke 伪造成
  "内容被改过"。需要 acl_version 的表在阶段 A5 显式声明该列。
- **CAS 语句不在这里**：``WHERE id=? AND version=? RETURNING`` 属于阶段 B2 的
  ``VersionedMixin``（Repository 能力）。本模块只负责列的语义、默认值与不变量，
  避免出现"两套乐观锁实现"。
- **时间戳统一 UTC**：连接层已强制 ``timezone=UTC``，``onupdate`` 与触发器都写
  ``now()``，因此 ``created_at``/``updated_at`` 恒为 timestamptz。
- **软删除两个时刻互相独立**：``deleted_at``（主动删除）与 ``archived_at``
  （归档）不互相覆盖，状态由 :class:`SoftDeleteState` 表达。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Uuid, event, func, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Table

from autumn_backend.db.base import Base
from autumn_backend.db.timestamps import updated_at_trigger_statements

__all__ = [
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
    """UUID 主键。

    只有真正使用 UUID 主键的表才继承它。``Setting`` 等 str 主键表**不**套用。
    数据库主键名由 ``pk_%(table_name)s`` 命名约定生成。
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class Versioned:
    """乐观并发版本列。

    语义边界（架构文档 §5）：``version`` 只在**私人内容或 metadata 变化**时递增
    （编辑原稿、标题、标签、私密备注、归档）。publish / revoke / 软删除 / 恢复
    只递增 ``acl_version``，**不**递增 ``version``。

    CAS 由阶段 B2 的 Repository 单语句完成::

        UPDATE ... SET version = :expected + 1, ...
        WHERE id = :id AND version = :expected
        RETURNING ...            -- 0 行 -> OptimisticLockError

    这里保留 :meth:`version_matches`，让 service 在不写 SQL 的前提下表达
    "我基于哪个版本做判断"；真正的并发仲裁仍然只有数据库语句能给出。
    """

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def version_matches(self, expected_version: int) -> bool:
        """当前内存对象的版本是否等于调用方持有的版本。"""
        return int(self.version) == int(expected_version)


class Timestamped:
    """``created_at`` / ``updated_at``，均为 UTC timestamptz。

    ``updated_at`` 双重保障：ORM 的 ``onupdate`` + 数据库 BEFORE UPDATE 触发器
    （见 ``db/timestamps.py``）。触发器在 ``Table.after_create`` 时挂载，
    因此由 Alembic 迁移建表后还需在迁移里显式挂一次——
    :func:`timestamped_tables` 给迁移脚本提供需要挂触发器的表清单。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SoftDelete:
    """软删除与归档。

    定位：软删除**只改可见性**，不删数据（架构文档 §5：publish/revoke/软删除/
    恢复都只递增 ``acl_version``）。默认不自动删除永久保存的业务记录。
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

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
def _attach_updated_at_trigger(
    target: Table,
    connection: Connection,
    **_kwargs: object,
) -> None:
    """任何带 ``updated_at`` 的表在创建后自动挂上时间戳触发器。

    SQLAlchemy 模型元数据里没有触发器概念，因此这是唯一的声明式挂载点。
    Alembic 迁移建表时不会触发该事件，迁移脚本需自行调用
    ``updated_at_trigger_sql(table)``（见 A3/A4）。
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
