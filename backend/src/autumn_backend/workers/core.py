"""领取事务结束后启动处理器及独立续租；成功结算由业务服务承担。"""

import asyncio
from contextlib import suppress
from typing import Any

from autumn_backend.errors import LeaseLostError
from autumn_backend.io_boundary import require_outside_uow
from autumn_backend.jobs.queue import LeasedJob, Queue
from autumn_backend.workers.registry import FailureHandler, Registry


class Worker:
    def __init__(
        self,
        queue: Queue,
        registry: Registry,
        failure: FailureHandler,
        *,
        heartbeat_seconds: float = 15,
        poll_seconds: float = 1,
    ) -> None:
        if not 0 < heartbeat_seconds < queue.lease_seconds / 2 or poll_seconds <= 0:
            raise ValueError("续租周期必须短于半个租期，轮询周期须为正数")
        self.queue, self.registry, self.failure = queue, registry, failure
        self.heartbeat_seconds, self.poll_seconds = heartbeat_seconds, poll_seconds

    async def _heartbeat(self, job: LeasedJob) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await self.queue.heartbeat(job)

    @staticmethod
    async def _cancel(task: asyncio.Task[Any]) -> None:
        task.cancel()
        # 不等待无限期不合作的外部请求；提交资格仍由数据库 lease 闸门控制。
        done, _ = await asyncio.wait({task}, timeout=2)
        if done:
            with suppress(asyncio.CancelledError, Exception):
                task.result()
        else:
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    async def run_once(self) -> bool:
        require_outside_uow()
        job = await self.queue.claim(self.registry.kinds)
        if job is None:
            return False

        async def invoke() -> None:
            await self.registry.handler(job)(job)

        handler = asyncio.create_task(invoke())
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            done, _ = await asyncio.wait({handler, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await self._cancel(handler)
                # 成功服务已经原子 finish 后的续租失败属于收尾竞争。
                if not await self.queue.settled(job):
                    heartbeat.result()
            else:
                await self._cancel(heartbeat)
                handler.result()
                if not await self.queue.settled(job):
                    raise RuntimeError("处理器返回前必须联合结算任务")
        except LeaseLostError:
            pass  # 已丧失资格，任何错误收尾也不能使用旧 token。
        except Exception as error:
            with suppress(LeaseLostError):
                await self.failure(job, error)
        finally:
            await self._cancel(heartbeat)
            if not handler.done():
                await self._cancel(handler)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            once = asyncio.create_task(self.run_once())
            stopping = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait({once, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    await self._cancel(once)
                    return
                once.result()
            finally:
                await self._cancel(stopping)
            if not once.result():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
