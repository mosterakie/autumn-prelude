"""持久来源的当前权限装配；不把历史、摘要或记忆视作天然可信文本。"""

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select

from autumn_backend.db.enums import RunSourceType
from autumn_backend.db.models import ResourceVersion, Run, RunSource
from autumn_backend.db.session import UnitOfWork
from autumn_backend.errors import NotFoundError, OptimisticLockError
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import (
    ContextFacts,
    ConversationMode,
    Operation,
    PolicyFacts,
    PublicationFacts,
    ResourceFacts,
    RevisionFacts,
    SourceFacts,
    SourceScope,
    TargetFacts,
    TargetKind,
    WebSourceFacts,
)
from autumn_backend.services.access import lock_authentication, require_allowed


@dataclass(frozen=True, slots=True)
class TaskFence:
    job_id: UUID
    token: UUID
    generation: int


async def require_task(uow: UnitOfWork, actor: ActorContext, run: Run, fence: TaskFence) -> None:
    """业务服务已按 user/session/run/resource 顺序加锁，再在同一事务校验任务资格。"""
    job = await uow.repositories.jobs.require_lease(fence.job_id, fence.token)
    payload = job.payload or {}
    if (
        job.kind not in ("run.dispatch", "run.resume")
        or job.run_id != run.id
        or job.actor_id != actor.user_id
        or job.auth_session_id != actor.auth_session_id
        or run.auth_session_id != actor.auth_session_id
    ):
        raise NotFoundError("运行任务不存在")
    if (
        fence.generation != run.execution_generation
        or type(payload.get("execution_generation")) is not int
        or payload["execution_generation"] != run.execution_generation
    ):
        raise OptimisticLockError("任务代际已失效")


async def source_fact(
    uow: UnitOfWork, source: RunSource, owner_id: UUID
) -> SourceFacts | WebSourceFacts:
    if source.source_type is RunSourceType.WEB:
        return WebSourceFacts(source_id=source.id, owner_id=owner_id)
    assert source.resource_id is not None and source.revision_id is not None
    resource = await uow.repositories.resources.get_for_update(source.resource_id)
    revision = await uow.session.scalar(
        select(ResourceVersion).where(
            ResourceVersion.id == source.revision_id,
            ResourceVersion.resource_id == source.resource_id,
        )
    )
    current = await uow.repositories.publications.current_for_resource(source.resource_id)
    facts = None
    if resource is not None and resource.current_revision_id is not None:
        facts = ResourceFacts(
            resource_id=resource.id,
            owner_id=resource.owner_id,
            current_revision_id=resource.current_revision_id,
            acl_version=resource.acl_version,
            is_deleted=resource.deleted_at is not None,
            is_archived=resource.archived_at is not None,
            expires_at=resource.expires_at,
            publication=PublicationFacts(
                publication_id=current.id,
                resource_id=current.resource_id,
                revision_id=current.revision_id,
                is_current=True,
                ai_enabled=current.ai_enabled,
                raw_download_enabled=current.raw_download_enabled,
                revoked_at=current.revoked_at,
            )
            if current is not None
            else None,
        )
    return SourceFacts(
        source_id=source.id,
        scope=SourceScope.PUBLIC if source.publication_id is not None else SourceScope.OWNER,
        resource_id=source.resource_id,
        revision_id=source.revision_id,
        acl_version=source.observed_acl_version,
        publication_id=source.publication_id,
        resource=facts,
        revision=RevisionFacts(revision_id=revision.id, resource_id=revision.resource_id)
        if revision is not None
        else None,
    )


async def run_facts(
    uow: UnitOfWork,
    actor: ActorContext,
    run_id: UUID,
    operation: Operation,
    *,
    expected_version: int | None = None,
    expected_generation: int | None = None,
    require_context: bool = True,
    extra_resource_ids: tuple[UUID, ...] = (),
) -> tuple[ActorContext, PolicyFacts, Run]:
    auth = await lock_authentication(uow, actor)
    probe = await uow.repositories.runs.get(run_id)
    if probe is None or probe.user_id != actor.user_id:
        raise NotFoundError("运行不存在")
    conversation = await uow.repositories.conversations.for_user_for_update(
        probe.conversation_id, probe.user_id
    )
    run = await uow.repositories.runs.get_for_update_or_raise(run_id)
    if expected_version is not None and run.version != expected_version:
        raise OptimisticLockError("旧执行的运行版本已失效")
    if conversation is None:
        raise NotFoundError("会话不存在")
    sources = (
        await uow.repositories.knowledge.sources(
            run_id, context_generation=run.execution_generation
        )
        if require_context
        else ()
    )
    for resource_id in sorted(
        {source.resource_id for source in sources if source.resource_id is not None}
        | set(extra_resource_ids)
    ):
        await uow.repositories.resources.get_for_update(resource_id)
    dependencies = tuple([await source_fact(uow, source, run.user_id) for source in sources])
    epoch = await uow.repositories.settings.get_acl_epoch()
    current_actor = replace(actor, scope_epoch=epoch)
    manifest = run.config_snapshot.get("context_manifest", {})
    complete = (
        isinstance(manifest, dict)
        and manifest.get("schema_version") == 1
        and manifest.get("complete") is True
        and manifest.get("context_generation") == run.execution_generation
    )
    context = ContextFacts(
        run_id=run.id,
        mode=ConversationMode(conversation.mode.value),
        captured_scope_epoch=run.scope_epoch,
        captured_generation=run.execution_generation
        if expected_generation is None
        else expected_generation,
        current_generation=run.execution_generation,
        sources=dependencies,
        sources_complete=complete,
    )
    facts = PolicyFacts(
        operation=operation,
        authentication=auth,
        now=await uow.repositories.users.database_time(),
        current_scope_epoch=epoch,
        target=TargetFacts(
            object_id=run.id,
            kind=TargetKind.RUN,
            owner_id=run.user_id,
            mode=ConversationMode(conversation.mode.value),
            is_deleted=conversation.deleted_at is not None,
            expires_at=conversation.expires_at,
        ),
        context=context if require_context else None,
    )
    require_allowed(current_actor, facts)
    return current_actor, facts, run


async def advance_context_generation(uow: UnitOfWork, run: Run, *, preserve_sources: bool) -> None:
    """调用方持有 Run 锁；等待/恢复复制已验证闭包，失效重建保留旧记录但不复用。"""
    sources = (
        await uow.repositories.knowledge.sources(
            run.id, context_generation=run.execution_generation
        )
        if preserve_sources
        else ()
    )
    run.execution_generation += 1
    for source in sources:
        values = {
            name: getattr(source, name)
            for name in (
                "source_type",
                "resource_id",
                "revision_id",
                "publication_id",
                "index_id",
                "chunk_id",
                "observed_acl_version",
                "locator",
                "web_url",
                "web_title",
                "fetched_at",
                "excerpt",
            )
        }
        key = (
            source.source_key.split(":", 1)[1]
            if source.source_key.startswith(f"g{source.context_generation}:")
            else source.source_key
        )
        await uow.repositories.knowledge.record_values(
            run.id, key, values, context_generation=run.execution_generation
        )
    if not preserve_sources:
        run.config_snapshot = {
            **run.config_snapshot,
            "context_manifest": {
                "schema_version": 1,
                "complete": False,
                "context_generation": run.execution_generation,
            },
        }
    else:
        run.config_snapshot = {
            **run.config_snapshot,
            "context_manifest": {
                **run.config_snapshot.get("context_manifest", {}),
                "context_generation": run.execution_generation,
            },
        }
