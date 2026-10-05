"""站长记忆的内容 CAS；授权与派生状态失效由应用服务承担。"""

from uuid import UUID

from sqlalchemy import func

from autumn_backend.db.enums import MemoryKind
from autumn_backend.db.models import Memory, Run, User
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.repositories.base import VersionedRepository
from autumn_backend.repositories.constraints import database_errors


class MemoryRepository(VersionedRepository[Memory]):
    model = Memory
    mutable_fields = frozenset({"content_text", "kind", "deleted_at"})

    async def create(
        self,
        *,
        user_id: UUID,
        kind: MemoryKind,
        content_text: str,
        origin_run_id: UUID | None = None,
    ) -> Memory:
        if not content_text or await self.session.get(User, user_id) is None:
            raise InvalidInputError("记忆内容或归属无效")
        if origin_run_id is not None:
            run = await self.session.get(Run, origin_run_id)
            if run is None or run.user_id != user_id:
                raise ConflictError("记忆来源归属不匹配")
        memory = Memory(
            user_id=user_id, kind=kind, content_text=content_text, origin_run_id=origin_run_id
        )
        with database_errors():
            self.session.add(memory)
            await self.session.flush()
        return memory

    async def revise(
        self, memory_id: UUID, *, expected_version: int, content_text: str, kind: MemoryKind
    ) -> Memory:
        memory = await self.get_for_update_or_raise(memory_id)
        if memory.deleted_at is not None:
            raise ConflictError("记忆已删除")
        return await self._update_versioned(
            memory_id, expected_version, {"content_text": content_text, "kind": kind}
        )

    async def soft_delete(self, memory_id: UUID, *, expected_version: int) -> Memory:
        return await self._update_versioned(
            memory_id, expected_version, {"deleted_at": func.clock_timestamp()}
        )
