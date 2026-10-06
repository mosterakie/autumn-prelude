"""进程启动时固定处理器，队列基础设施不依赖本模块。"""

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType

from autumn_backend.jobs.queue import LeasedJob

Handler = Callable[[LeasedJob], Awaitable[None]]
FailureHandler = Callable[[LeasedJob, Exception], Awaitable[None]]


class Registry:
    def __init__(self, handlers: Mapping[str, Handler]) -> None:
        if not handlers or any(not key or not callable(value) for key, value in handlers.items()):
            raise ValueError("Worker 必须配置至少一个有效处理器")
        self.handlers = MappingProxyType(dict(handlers))
        self.kinds = tuple(self.handlers)

    def handler(self, job: LeasedJob) -> Handler:
        return self.handlers[job.kind]
