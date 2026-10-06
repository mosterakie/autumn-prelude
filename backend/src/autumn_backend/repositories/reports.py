"""同一用户/留言的未处理举报去重与版本化处理。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import ReportStatus
from autumn_backend.db.models import Report
from autumn_backend.errors import ConflictError, IdempotencyConflictError
from autumn_backend.repositories.base import VersionedRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.pagination import Page, fetch_page
from autumn_backend.repositories.result import Creation


class ReportRepository(VersionedRepository[Report]):
    model = Report
    mutable_fields = frozenset({"status", "handled_by", "resolution_note", "resolved_at"})

    async def create_or_get(
        self, reporter_id: UUID, comment_id: UUID, reason: str
    ) -> Creation[Report]:
        with database_errors():
            report = (
                await self.session.execute(
                    insert(Report)
                    .values(
                        reporter_id=reporter_id,
                        comment_id=comment_id,
                        reason=reason,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[Report.reporter_id, Report.comment_id],
                        index_where=Report.status == ReportStatus.OPEN,
                    )
                    .returning(Report)
                )
            ).scalar_one_or_none()
        if report is not None:
            return Creation(report, True)
        report = (
            await self.session.execute(
                select(Report)
                .where(
                    Report.reporter_id == reporter_id,
                    Report.comment_id == comment_id,
                    Report.status == ReportStatus.OPEN,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if report is None:
            raise ConflictError("举报状态已变化，请重试")
        if report.reason != reason:
            raise IdempotencyConflictError("同一留言已有不同内容的待处理举报")
        return Creation(report, False)

    async def page(self, *, status: ReportStatus, limit: int, cursor: str | None) -> Page[Report]:
        return await fetch_page(
            self.session,
            Report,
            select(Report).where(Report.status == status),
            limit=limit,
            cursor=cursor,
        )

    async def resolve(
        self,
        report_id: UUID,
        expected_version: int,
        moderator_id: UUID,
        resolution: str,
        *,
        dismissed: bool,
    ) -> Report:
        return await self._update_versioned(
            report_id,
            expected_version,
            {
                "status": ReportStatus.DISMISSED if dismissed else ReportStatus.RESOLVED,
                "handled_by": moderator_id,
                "resolution_note": resolution,
                "resolved_at": await self.database_time(),
            },
        )
