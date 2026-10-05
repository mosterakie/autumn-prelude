from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from autumn_backend.policies.actor import ActorContext, ActorRole
from autumn_backend.policies.decision import DenialCode
from autumn_backend.policies.facts import (
    ContextFacts,
    ConversationMode,
    Operation,
    PolicyFacts,
    SourceFacts,
    TargetKind,
    WebSourceFacts,
)
from autumn_backend.policies.policy import evaluate
from tests.unit.policy_helpers import NOW, facts, identity, resource, source, target

pytestmark = pytest.mark.unit


def request(*, private: bool = False) -> tuple[ActorContext, PolicyFacts]:
    actor, auth = identity(ActorRole.OWNER if private else ActorRole.MEMBER, step_up=private)
    obj = target(
        actor, TargetKind.RUN, ConversationMode.OWNER if private else ConversationMode.PUBLIC
    )
    captured = source(resource(actor, published=not private), private=private)
    context = ContextFacts(
        run_id=obj.object_id,
        mode=obj.mode,
        captured_scope_epoch=3,
        captured_generation=1,
        current_generation=1,
        sources=(captured,),
        sources_complete=True,
    )
    return actor, replace(facts(Operation.CONTINUE_RUN, auth, obj=obj), context=context)


@pytest.mark.parametrize(
    "operation", [Operation.RESUME_RUN, Operation.CONTINUE_RUN, Operation.EMIT_RUN_OUTPUT]
)
@pytest.mark.parametrize("private", [False, True])
def test_current_context_can_continue_and_emit(operation: Operation, private: bool) -> None:
    actor, data = request(private=private)
    assert evaluate(actor, replace(data, operation=operation)).allowed


@pytest.mark.parametrize(
    "changes",
    [
        {"captured_scope_epoch": 2},
        {"current_generation": 2},
        {"captured_generation": 0},
        {"run_id": uuid4()},
        {"mode": ConversationMode.OWNER},
        {"sources_complete": False},
    ],
)
def test_stale_context_is_invalidated(changes: dict[str, object]) -> None:
    actor, data = request()
    assert data.context is not None
    data = replace(data, context=replace(data.context, **changes))
    decision = evaluate(actor, data)
    assert decision.code is DenialCode.ACL_CONTEXT_INVALIDATED
    assert decision.http_status == 409


def test_missing_context_and_missing_dependency_closure_fail_closed() -> None:
    actor, data = request()
    assert evaluate(actor, replace(data, context=None)).code is DenialCode.ACL_CONTEXT_INVALIDATED
    assert data.context is not None
    empty = replace(data.context, sources=(), sources_complete=True)
    assert evaluate(actor, replace(data, context=empty)).allowed
    assert (
        evaluate(actor, replace(data, context=replace(empty, sources_complete=False))).code
        is DenialCode.ACL_CONTEXT_INVALIDATED
    )
    assert (
        evaluate(actor, replace(data, operation=Operation.RESUME_RUN, context=None)).code
        is DenialCode.ACL_CONTEXT_INVALIDATED
    )


def test_epoch_change_stops_old_generation_even_if_source_was_republished() -> None:
    actor, data = request()
    assert data.context is not None
    # 撤回后重授予也递增 epoch；不能因当前可读便恢复旧 checkpoint。
    actor = replace(actor, scope_epoch=5)
    data = replace(data, current_scope_epoch=5)
    assert evaluate(actor, data).code is DenialCode.ACL_CONTEXT_INVALIDATED
    rebuilt = replace(
        data.context, captured_scope_epoch=5, captured_generation=2, current_generation=2
    )
    assert evaluate(actor, replace(data, context=rebuilt)).allowed
    # 重建后的新代际不能接受仍在飞行中的旧输出。
    old_output = replace(rebuilt, captured_generation=1)
    assert (
        evaluate(actor, replace(data, operation=Operation.EMIT_RUN_OUTPUT, context=old_output)).code
        is DenialCode.ACL_CONTEXT_INVALIDATED
    )


def test_actor_epoch_also_requires_current_server_reconstruction() -> None:
    actor, data = request()
    assert evaluate(replace(actor, scope_epoch=2), data).code is DenialCode.ACL_CONTEXT_INVALIDATED


@pytest.mark.parametrize(
    "changes",
    [
        {"is_deleted": True},
        {"is_archived": True},
        {"expires_at": NOW},
        {"publication": None},
        {"acl_version": 3},
    ],
)
def test_any_revoked_dependency_invalidates_generated_context(changes: dict[str, object]) -> None:
    actor, data = request()
    assert data.context is not None
    captured = data.context.sources[0]
    assert isinstance(captured, SourceFacts) and captured.resource is not None
    captured = replace(captured, resource=replace(captured.resource, **changes))
    data = replace(data, context=replace(data.context, sources=(captured,)))
    assert evaluate(actor, data).code is DenialCode.ACL_CONTEXT_INVALIDATED


@pytest.mark.parametrize(
    "change",
    [
        "missing_resource",
        "missing_revision",
        "wrong_resource",
        "wrong_revision",
        "wrong_publication",
        "ai_disabled",
    ],
)
def test_source_identity_and_current_projection_are_checked(change: str) -> None:
    actor, data = request()
    assert data.context is not None
    captured = data.context.sources[0]
    assert isinstance(captured, SourceFacts) and captured.resource is not None
    assert captured.revision is not None and captured.resource.publication is not None
    match change:
        case "missing_resource":
            captured = replace(captured, resource=None)
        case "missing_revision":
            captured = replace(captured, revision=None)
        case "wrong_resource":
            captured = replace(captured, resource_id=uuid4())
        case "wrong_revision":
            captured = replace(captured, revision=replace(captured.revision, revision_id=uuid4()))
        case "wrong_publication":
            captured = replace(captured, publication_id=uuid4())
        case "ai_disabled":
            publication = replace(captured.resource.publication, ai_enabled=False)
            captured = replace(
                captured, resource=replace(captured.resource, publication=publication)
            )
    data = replace(data, context=replace(data.context, sources=(captured,)))
    assert evaluate(actor, data).code is DenialCode.ACL_CONTEXT_INVALIDATED


def test_historical_private_revision_remains_usable_with_current_permission() -> None:
    actor, data = request(private=True)
    assert data.context is not None
    captured = data.context.sources[0]
    assert isinstance(captured, SourceFacts) and captured.resource is not None
    captured = replace(captured, resource=replace(captured.resource, current_revision_id=uuid4()))
    data = replace(data, context=replace(data.context, sources=(captured,)))
    assert evaluate(actor, data).allowed


def test_private_source_cannot_be_relabelled_as_public_even_after_publication() -> None:
    actor, data = request(private=True)
    assert data.context is not None and data.target is not None
    captured = data.context.sources[0]
    assert isinstance(captured, SourceFacts)
    assert captured.resource is not None
    public = resource(actor)
    # 用相同资源身份的当前公开投影，仍保留原 private chunk 的 scope。
    assert public.publication is not None
    projection = replace(
        public.publication, resource_id=captured.resource_id, revision_id=captured.revision_id
    )
    captured = replace(captured, resource=replace(captured.resource, publication=projection))
    assert evaluate(
        actor, replace(data, context=replace(data.context, sources=(captured,)))
    ).allowed
    public_target = replace(data.target, mode=ConversationMode.PUBLIC)
    public_context = replace(data.context, mode=ConversationMode.PUBLIC, sources=(captured,))
    assert (
        evaluate(actor, replace(data, target=public_target, context=public_context)).code
        is DenialCode.ACL_CONTEXT_INVALIDATED
    )


def test_unlisted_indirect_summary_or_memory_source_is_still_a_dependency() -> None:
    actor, data = request()
    assert data.context is not None
    # 已可见引用以外，历史回答/摘要/记忆实际输入的来源也必须加入闭包。
    dependency = source(resource(actor))
    assert dependency.resource is not None
    dependency = replace(dependency, resource=replace(dependency.resource, is_deleted=True))
    context = replace(data.context, sources=(*data.context.sources, dependency))
    assert (
        evaluate(actor, replace(data, context=context)).code is DenialCode.ACL_CONTEXT_INVALIDATED
    )


def test_source_expiration_invalidates_context_without_epoch_bump() -> None:
    actor, data = request()
    assert data.context is not None
    captured = data.context.sources[0]
    assert isinstance(captured, SourceFacts) and captured.resource is not None
    expiry = NOW + timedelta(seconds=1)
    captured = replace(captured, resource=replace(captured.resource, expires_at=expiry))
    data = replace(data, context=replace(data.context, sources=(captured,)))
    assert evaluate(actor, data).allowed
    assert evaluate(actor, replace(data, now=expiry)).code is DenialCode.ACL_CONTEXT_INVALIDATED


def test_web_sources_require_current_owner_and_step_up_even_in_public_mode() -> None:
    actor, auth = identity(ActorRole.OWNER, step_up=True)
    assert actor.user_id is not None
    obj = target(actor, TargetKind.RUN)
    web = WebSourceFacts(source_id=uuid4(), owner_id=actor.user_id)
    context = ContextFacts(
        run_id=obj.object_id,
        mode=ConversationMode.PUBLIC,
        captured_scope_epoch=3,
        captured_generation=0,
        current_generation=0,
        sources=(web,),
        sources_complete=True,
    )
    data = replace(facts(Operation.EMIT_RUN_OUTPUT, auth, obj=obj), context=context)
    assert evaluate(actor, data).allowed
    assert actor.step_up_expires_at is not None
    assert (
        evaluate(actor, replace(data, now=actor.step_up_expires_at)).code
        is DenialCode.STEP_UP_REQUIRED
    )
    context = replace(context, sources=(replace(web, owner_id=uuid4()),))
    assert (
        evaluate(actor, replace(data, context=context)).code is DenialCode.ACL_CONTEXT_INVALIDATED
    )


def test_authentication_and_run_ownership_precede_context_invalidation_details() -> None:
    actor, data = request(private=True)
    assert data.authentication is not None and data.context is not None
    data = replace(data, current_scope_epoch=4)
    revoked = replace(data.authentication, revoked_at=NOW)
    assert evaluate(actor, replace(data, authentication=revoked)).code is DenialCode.SESSION_EXPIRED
    assert (
        evaluate(replace(actor, step_up_expires_at=None), data).code is DenialCode.STEP_UP_REQUIRED
    )
    other, _ = identity(ActorRole.OWNER, step_up=True)
    assert evaluate(other, data).code is DenialCode.NOT_FOUND


def test_citation_never_substitutes_new_publication_for_captured_one() -> None:
    actor, data = request()
    assert data.context is not None
    captured = data.context.sources[0]
    assert isinstance(captured, SourceFacts) and captured.resource is not None
    assert captured.resource.publication is not None
    citation = replace(
        target(actor, TargetKind.CITATION),
        object_id=captured.source_id,
        resource_id=captured.resource_id,
    )
    data = replace(
        data, operation=Operation.READ_CITATION, target=citation, context=None, source=captured
    )
    assert evaluate(actor, data).allowed
    publication = replace(captured.resource.publication, publication_id=uuid4())
    captured = replace(captured, resource=replace(captured.resource, publication=publication))
    assert evaluate(actor, replace(data, source=captured)).code is DenialCode.NOT_FOUND


def test_context_cannot_accept_mutable_or_duplicate_dependency_list() -> None:
    _, data = request()
    assert data.context is not None
    with pytest.raises(ValueError, match="tuple"):
        replace(data.context, sources=list(data.context.sources))
    with pytest.raises(ValueError, match="deduplicated"):
        replace(data.context, sources=data.context.sources * 2)
