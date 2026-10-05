"""首版本地对象适配器：固定 object key、摘要校验、幂等原子转正。"""

import asyncio
import hashlib
import os
import re
from pathlib import Path

from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.io_boundary import require_outside_uow


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str, *, staging: bool = False) -> Path:
        if not re.fullmatch(r"objects/[0-9a-f]{32}\.(pdf|docx)", key):
            raise InvalidInputError("对象标识无效")
        value = self.root / (key.replace("objects/", "staging/", 1) if staging else key)
        if not value.resolve().is_relative_to(self.root):
            raise InvalidInputError("对象路径越界")
        return value

    async def stage(self, key: str, data: bytes) -> None:
        require_outside_uow()
        await asyncio.to_thread(self._stage, key, data)

    def _stage(self, key: str, data: bytes) -> None:
        target = self._path(key, staging=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise ConflictError("暂存对象已有不同内容")
            os.utime(target, None)
            return
        try:
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if target.read_bytes() != data:
                raise ConflictError("暂存对象已有不同内容") from None

    async def promote(self, key: str, digest: str, size: int) -> None:
        require_outside_uow()
        await asyncio.to_thread(self._promote, key, digest, size)

    def _promote(self, key: str, digest: str, size: int) -> None:
        source, target = self._path(key, staging=True), self._path(key)
        candidate = target if target.exists() else source
        data = candidate.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ConflictError("对象校验失败")
        if candidate == source:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        elif source.exists():
            source.unlink()

    async def read(self, key: str) -> bytes:
        require_outside_uow()
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        require_outside_uow()
        await asyncio.to_thread(self._delete, key)

    def _delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
        self._path(key, staging=True).unlink(missing_ok=True)

    async def orphan_candidates(self, before_timestamp: float) -> tuple[str, ...]:
        require_outside_uow()
        return await asyncio.to_thread(self._orphans, before_timestamp)

    def _orphans(self, before_timestamp: float) -> tuple[str, ...]:
        path = self.root / "staging"
        if not path.resolve().is_relative_to(self.root):
            raise InvalidInputError("暂存目录越界")
        if not path.exists():
            return ()
        return tuple(
            "objects/" + value.name
            for value in path.iterdir()
            if re.fullmatch(r"[0-9a-f]{32}\.(pdf|docx)", value.name)
            and value.is_file()
            and value.stat().st_mtime < before_timestamp
        )

    async def remove_orphan(self, key: str, before_timestamp: float) -> None:
        require_outside_uow()
        await asyncio.to_thread(self._remove_orphan, key, before_timestamp)

    def _remove_orphan(self, key: str, before_timestamp: float) -> None:
        target = self._path(key, staging=True)
        if target.exists() and target.stat().st_mtime < before_timestamp:
            target.unlink()
