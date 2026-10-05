"""持久来源的当前权限装配；不把历史、摘要或记忆视作天然可信文本。"""

from dataclasses import replace
from uuid import UUID

from sqlalchemy import select

from autumn_backend.db.enums import RunSourceType
from autumn_backend.db.models import ResourceVersion, Run, RunSource
from autumn_backend.db.session import UnitOfWork
from autumn_backend.errors import NotFoundError
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
    require_context: bool = True,
) -> tuple[ActorContext, PolicyFacts, Run]:
    auth = await lock_authentication(uow, actor)
    probe = await uow.repositories.runs.get(run_id)
    if probe is None or probe.user_id != actor.user_id:
        raise NotFoundError("运行不存在")
    conversation = await uow.repositories.conversations.for_user_for_update(
        probe.conversation_id, probe.user_id
    )
    run = await uow.repositories.runs.get_for_update_or_raise(run_id)
    if conversation is None:
        raise NotFoundError("会话不存在")
    sources = await uow.repositories.knowledge.sources(run_id)
    for resource_id in sorted(
        {source.resource_id for source in sources if source.resource_id is not None}
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
    )
    context = ContextFacts(
        run_id=run.id,
        mode=ConversationMode(conversation.mode.value),
        captured_scope_epoch=run.scope_epoch,
        captured_generation=run.version if expected_version is None else expected_version,
        current_generation=run.version,
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
