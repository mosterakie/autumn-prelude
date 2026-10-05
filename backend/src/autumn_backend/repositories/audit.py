"""专用只追加审计接口；元数据仅含对象身份、字段名、状态码与版本。"""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from autumn_backend.db.enums import AuditResult
from autumn_backend.db.models import AuditEvent
from autumn_backend.errors import InvalidInputError
from autumn_backend.repositories.base import AppendOnlyRepository
from autumn_backend.repositories.constraints import database_errors


@dataclass(frozen=True, slots=True)
class AuditMetadata:
    before_acl_version: int | None = None
    after_acl_version: int | None = None
    scope_epoch: int | None = None
    changed_fields: tuple[str, ...] = ()
    before_status: str | None = None
    after_status: str | None = None
    reason_code: str | None = None
    settings_key: str | None = None
    publication_id: UUID | None = None
    run_id: UUID | None = None
    job_id: UUID | None = None
    comment_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in ("before_acl_version", "after_acl_version", "scope_epoch"):
            value = getattr(self, name)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise InvalidInputError("审计版本无效")
                values[name] = value
        for name in ("before_status", "after_status", "reason_code", "settings_key"):
            code = getattr(self, name)
            if code is not None:
                if not re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", code):
                    raise InvalidInputError("审计元数据只接受受控状态码和字段名")
                values[name] = code
        if self.changed_fields:
            if len(self.changed_fields) > 32 or any(
                not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in self.changed_fields
            ):
                raise InvalidInputError("审计字段名无效")
            values["changed_fields"] = list(self.changed_fields)
        for name in ("publication_id", "run_id", "job_id", "comment_id"):
            identifier = getattr(self, name)
            if identifier is not None:
                values[name] = str(identifier)
        return values


class AuditEventRepository(AppendOnlyRepository):
    async def record(
        self,
        *,
        event_type: str,
        result: AuditResult,
        actor_id: UUID | None = None,
        action_id: UUID | None = None,
        resource_id: UUID | None = None,
        before_version: int | None = None,
        after_version: int | None = None,
        request_id: str | None = None,
        metadata: AuditMetadata | None = None,
    ) -> AuditEvent:
        if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", event_type):
            raise InvalidInputError("审计事件类型无效")
        if any(
            version is not None and (type(version) is not int or version < 0)
            for version in (before_version, after_version)
        ):
            raise InvalidInputError("审计内容版本无效")
        event = AuditEvent(
            event_type=event_type,
            result=result,
            actor_id=actor_id,
            action_id=action_id,
            resource_id=resource_id,
            before_version=before_version,
            after_version=after_version,
            request_id=request_id,
            metadata_json=metadata.to_dict() if metadata is not None else {},
        )
        with database_errors():
            self.session.add(event)
            await self.session.flush()
        return event
