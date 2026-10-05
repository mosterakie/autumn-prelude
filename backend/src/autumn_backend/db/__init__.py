"""db 模块。

ORM 基础设施：DeclarativeBase、命名约定、Mixin、异步会话与 UoW、Alembic 迁移。

阶段 A1：``base`` / ``session`` / ``health``
阶段 A2：``mixins`` / ``naming`` / ``timestamps``
阶段 A3 起：``models``（逐表声明与约束）
"""

from autumn_backend.db.base import NAMING_CONVENTION, Base
from autumn_backend.db.mixins import (
    SoftDelete,
    SoftDeleteState,
    Timestamped,
    UUIDPrimaryKey,
    Versioned,
    timestamped_tables,
)
from autumn_backend.db.naming import (
    check_constraint_name,
    index_name,
    unique_constraint_name,
)
from autumn_backend.db.session import (
    UnitOfWork,
    UnitOfWorkFactory,
    create_engine,
    create_session_factory,
    run_in_uow,
)

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "SoftDelete",
    "SoftDeleteState",
    "Timestamped",
    "UUIDPrimaryKey",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "Versioned",
    "check_constraint_name",
    "create_engine",
    "create_session_factory",
    "index_name",
    "run_in_uow",
    "timestamped_tables",
    "unique_constraint_name",
]
