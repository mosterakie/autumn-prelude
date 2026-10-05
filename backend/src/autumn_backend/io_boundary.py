"""应用 UoW 的协程级边界；子任务继承上下文，不能偷偷在事务中执行外部 I/O。"""

from contextvars import ContextVar

active_uows: ContextVar[int] = ContextVar("autumn_active_uows", default=0)


def require_outside_uow() -> None:
    if active_uows.get():
        raise RuntimeError("外部 I/O 必须在 UnitOfWork 之外执行")
