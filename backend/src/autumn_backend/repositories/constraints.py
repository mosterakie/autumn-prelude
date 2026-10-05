"""只读取 PostgreSQL 诊断字段，不解析或向用户暴露 SQL/参数。"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from autumn_backend.errors import ConflictError, DomainError, InvalidInputError


class ActiveRunConflictError(ConflictError):
    code = "conversation_busy"


class ConstraintViolationError(DomainError):
    code = "constraint_violation"


def translate_constraint(error: IntegrityError) -> DomainError:
    original = error.orig
    state = getattr(original, "sqlstate", None)
    cause = getattr(original, "__cause__", None)
    constraint = getattr(cause, "constraint_name", None)
    if state == "23505":
        if constraint == "uq_runs_conversation_id_non_terminal":
            return ActiveRunConflictError("当前会话已有未结束的运行")
        return ConflictError("对象与已有记录冲突")
    if state == "23503":
        return ConflictError("关联对象不存在、已变化或归属不匹配")
    if state in {"23502", "23514"}:
        return InvalidInputError("数据不满足存储约束")
    return ConstraintViolationError("数据库约束拒绝了操作")


@contextmanager
def database_errors() -> Iterator[None]:
    """失败后事务仍需回滚；需局部恢复时，调用方显式使用 SAVEPOINT。"""
    try:
        yield
    except IntegrityError as error:
        raise translate_constraint(error) from error
