"""留言幂等及一级回复的事务内跨行校验。"""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.models import Comment
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.repositories.base import VersionedRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.result import Creation


def normalize_body(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n")


def request_hash(resource_id: UUID | None, parent_id: UUID | None, body: str) -> str:
    payload = {
        "resource_id": str(resource_id) if resource_id else None,
        "parent_id": str(parent_id) if parent_id else None,
        "body": normalize_body(body),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


class CommentRepository(VersionedRepository[Comment]):
    model = Comment

    @staticmethod
    def _replay(comment: Comment, digest: str) -> Creation[Comment]:
        if comment.request_hash != digest:
            raise ConflictError("留言标识已用于不同请求")
        return Creation(comment, False)

    async def create_or_get(
        self,
        *,
        author_id: UUID,
        client_id: UUID,
        body: str,
        resource_id: UUID | None = None,
        parent_id: UUID | None = None,
    ) -> Creation[Comment]:
        body = normalize_body(body)
        if not body:
            raise InvalidInputError("留言正文不能为空")
        digest = request_hash(resource_id, parent_id, body)
        identity = (
            select(Comment)
            .execution_options(populate_existing=True)
            .where(Comment.author_id == author_id, Comment.client_id == client_id)
        )
        existing = (await self.session.execute(identity)).scalar_one_or_none()
        if existing is not None:
            return self._replay(existing, digest)
        if parent_id is not None:
            parent = await self.get_for_update(parent_id)
            if (
                parent is None
                or parent.deleted_at is not None
                or parent.parent_id is not None
                or parent.resource_id != resource_id
            ):
                raise ConflictError("回复目标不存在、已删除或不属于当前留言范围")
        with database_errors():
            comment = (
                await self.session.execute(
                    insert(Comment)
                    .values(
                        author_id=author_id,
                        client_id=client_id,
                        body=body,
                        resource_id=resource_id,
                        parent_id=parent_id,
                        request_hash=digest,
                    )
                    .on_conflict_do_nothing(constraint="uq_comments_author_client")
                    .returning(Comment)
                )
            ).scalar_one_or_none()
        if comment is not None:
            return Creation(comment, True)
        existing = (await self.session.execute(identity)).scalar_one_or_none()
        if existing is None:
            raise ConflictError("留言已变化，请重试")
        return self._replay(existing, digest)
