"""受理速率的固定窗口计数；与业务事务共同提交或回滚。"""

from datetime import datetime, timedelta

from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.models import RateLimitBucket
from autumn_backend.errors import InvalidInputError, RateLimitedError
from autumn_backend.repositories.base import RepositoryBase
from autumn_backend.repositories.constraints import database_errors


class RateLimitRepository(RepositoryBase):
    async def consume(
        self,
        *,
        scope_hash: str,
        policy_key: str,
        window_start: datetime,
        window_end: datetime,
        limit: int,
    ) -> int:
        if (
            not scope_hash
            or not policy_key
            or type(limit) is not int
            or limit < 0
            or window_start.tzinfo is None
            or window_end.tzinfo is None
            or window_end <= window_start
        ):
            raise InvalidInputError("速率窗口无效")
        if limit == 0:
            raise RateLimitedError("当前受理速率已用完", retry_at=window_end)
        statement = (
            insert(RateLimitBucket)
            .values(
                scope_hash=scope_hash,
                policy_key=policy_key,
                window_start=window_start,
                window_end=window_end,
                hits=1,
                expires_at=window_end + timedelta(hours=1),
            )
            .on_conflict_do_update(
                constraint="pk_rate_limit_buckets",
                set_={"hits": RateLimitBucket.hits + 1},
                where=(RateLimitBucket.hits < limit) & (RateLimitBucket.window_end == window_end),
            )
            .returning(RateLimitBucket.hits)
        )
        with database_errors():
            hits = (await self.session.execute(statement)).scalar_one_or_none()
        if hits is None:
            raise RateLimitedError("当前受理速率已用完", retry_at=window_end)
        return int(hits)
