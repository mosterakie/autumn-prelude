"""SQLAlchemy 声明式基类与命名约定。

命名约定是 A4 的前提：没有固定命名约定，``alembic revision --autogenerate`` 之后
``alembic check`` 会因索引/约束默认名漂移而无法通过。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 显式命名约定，覆盖 UNIQUE / CHECK / FK / PK / INDEX 与 Partial Unique Index。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        identity = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identity!r}>"

    def to_dict(self) -> dict[str, Any]:
        """只用于日志与调试的可读快照；不用于序列化公开响应。"""
        return {column.key: getattr(self, column.key) for column in self.__table__.columns}
