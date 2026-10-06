"""站长联网搜索：先授权/记账，外部 I/O 后重验任务，再原子保存引用与结果。"""

import hashlib
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from autumn_backend.db.enums import ProviderCallPurpose, RunSourceType, RunStatus
from autumn_backend.db.models import Run
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.io_boundary import require_outside_uow
from autumn_backend.knowledge.web import WebPage, validate_url
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import ConversationMode, Operation, PolicyFacts
from autumn_backend.repositories.provider_calls import external_idempotency_key
from autumn_backend.services.access import require_allowed
from autumn_backend.services.context import TaskFence, require_task, run_facts
from autumn_backend.services.knowledge import Citation, KnowledgeService


@dataclass(frozen=True, slots=True)
class SearchResult:
    pages: tuple[WebPage, ...]
    external_request_id: str | None = None
    search_units: int | None = None


class SearchProvider(Protocol):
    provider: str
    model: str

    async def search(
        self, query: str, *, limit: int, external_idempotency_key: str
    ) -> SearchResult: ...


class WebSearchService:
    def __init__(self, uows: UnitOfWorkFactory, provider: SearchProvider) -> None:
        self.uows, self.provider = uows, provider

    async def _gate(
        self,
        uow: UnitOfWork,
        actor: ActorContext,
        run_id: UUID,
        fence: TaskFence,
        *,
        version: int | None = None,
    ) -> tuple[ActorContext, PolicyFacts, Run]:
        current_actor, facts, run = await run_facts(
            uow,
            actor,
            run_id,
            Operation.CONTINUE_RUN,
            expected_version=version,
            expected_generation=fence.generation,
        )
        require_allowed(current_actor, replace(facts, operation=Operation.SEARCH_WEB))
        if (
            facts.target is None
            or facts.target.mode is not ConversationMode.OWNER
            or run.config_snapshot.get("web_enabled") is not True
            or run.config_snapshot.get("search_mode") not in ("web", "auto")
            or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING)
        ):
            raise ConflictError("当前运行未授权联网搜索")
        await require_task(uow, current_actor, run, fence)
        return current_actor, facts, run

    async def search(
        self,
        actor: ActorContext,
        run_id: UUID,
        query: str,
        *,
        limit: int = 3,
        fence: TaskFence,
        step: int,
    ) -> tuple[Citation, ...]:
        if not query.strip() or len(query) > 8000 or not 1 <= limit <= 5 or step < 1:
            raise InvalidInputError("联网搜索输入无效")
        async with self.uows() as uow:
            _, _, run = await self._gate(uow, actor, run_id, fence)
            version = run.version
            call = await KnowledgeService._prepare_external(
                uow,
                provider=self.provider.provider,
                model=self.provider.model,
                purpose=ProviderCallPurpose.SEARCH,
                key=f"search:{run.id}:g{fence.generation}:step{step}",
                job_id=fence.job_id,
                run_id=run.id,
            )
            call_id, key = call.id, external_idempotency_key(call.logical_call_key)
        require_outside_uow()
        result = await self.provider.search(query, limit=limit, external_idempotency_key=key)
        if len(result.pages) > limit or any(
            not page.text.strip() or len(page.text) > 8000 for page in result.pages
        ):
            raise InvalidInputError("联网结果载荷无效")
        async with self.uows() as uow:
            current_actor, facts, run = await self._gate(uow, actor, run_id, fence, version=version)
            citations = []
            for page in result.pages:
                url = validate_url(page.url)
                source = await uow.repositories.knowledge.record_values(
                    run_id,
                    "web:" + hashlib.sha256(f"{url}\n{page.text}".encode()).hexdigest(),
                    {
                        "source_type": RunSourceType.WEB,
                        "observed_acl_version": 0,
                        "web_url": url,
                        "web_title": page.title[:300],
                        "fetched_at": facts.now,
                        "excerpt": page.text,
                        "locator": {"kind": "web", "url": url},
                    },
                    context_generation=run.execution_generation,
                )
                citations.append(Citation(source.id, page.text, None, None, None, source.locator))
            await uow.repositories.provider_calls.settle_succeeded(
                call_id,
                external_request_id=result.external_request_id,
                search_units=result.search_units,
            )
            await self._gate(uow, current_actor, run_id, fence, version=version)
            return tuple(citations)
