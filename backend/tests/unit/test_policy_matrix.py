from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from autumn_backend.policies.actor import ActorRole, Capability
from autumn_backend.policies.capabilities import capabilities_for
from autumn_backend.policies.decision import DenialCode
from autumn_backend.policies.facts import (
    AccountStatus,
    ContextFacts,
    ConversationMode,
    Operation,
    SearchMode,
    TargetKind,
)
from autumn_backend.policies.policy import evaluate
from tests.unit.policy_helpers import NOW, anonymous, facts, identity, resource, target

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "operation",
    [
        Operation.SEARCH_WEB,
        Operation.SEARCH_PRIVATE_KNOWLEDGE,
        Operation.CREATE_RESOURCE,
        Operation.INGEST_RESOURCE,
        Operation.MANAGE_SETTINGS,
        Operation.MANAGE_MODERATION,
        Operation.READ_AUDIT,
        Operation.CREATE_MEMORY,
    ],
)
@pytest.mark.parametrize("principal", ["anonymous", "member", "owner", "stepped_owner"])
def test_owner_operations_matrix(operation: Operation, principal: str) -> None:
    if principal == "anonymous":
        actor, auth = anonymous(), None
        expected = DenialCode.AUTH_REQUIRED
    elif principal == "member":
        actor, auth = identity()
        expected = DenialCode.FORBIDDEN
    else:
        actor, auth = identity(ActorRole.OWNER, step_up=principal == "stepped_owner")
        expected = None if principal == "stepped_owner" else DenialCode.STEP_UP_REQUIRED
    assert evaluate(actor, facts(operation, auth)).code is expected


def test_echoed_capabilities_do_not_grant_web_and_empty_hints_do_not_revoke_real_grants() -> None:
    actor, auth = identity()
    forged = replace(actor, capabilities=frozenset(Capability))
    assert evaluate(forged, facts(Operation.SEARCH_WEB, auth)).code is DenialCode.FORBIDDEN
    assert Capability.SEARCH_WEB not in capabilities_for(forged, facts(Operation.SEARCH_WEB, auth))
    owner, current = identity(ActorRole.OWNER, step_up=True)
    owner = replace(owner, capabilities=frozenset())
    assert evaluate(owner, facts(Operation.SEARCH_WEB, current)).allowed
    assert Capability.SEARCH_WEB in capabilities_for(owner, facts(Operation.SEARCH_WEB, current))


def test_capability_hints_change_at_step_up_expiry_and_account_disable() -> None:
    actor, auth = identity(ActorRole.OWNER, step_up=True)
    data = facts(Operation.SEARCH_WEB, auth)
    assert Capability.PRIVATE_KNOWLEDGE in capabilities_for(actor, data)
    assert actor.step_up_expires_at is not None
    expired = replace(data, now=actor.step_up_expires_at)
    assert Capability.PRIVATE_KNOWLEDGE not in capabilities_for(actor, expired)
    assert Capability.PUBLIC_AI in capabilities_for(actor, expired)
    disabled = replace(data, authentication=replace(auth, status=AccountStatus.DISABLED))
    assert capabilities_for(actor, disabled) == frozenset({Capability.READ_PUBLIC})


@pytest.mark.parametrize(
    "operation",
    [
        Operation.CREATE_COMMENT,
        Operation.CREATE_CONVERSATION,
        Operation.ASK,
        Operation.SEARCH_PUBLIC_KNOWLEDGE,
    ],
)
def test_email_verification_is_required_for_new_ai_and_comments(operation: Operation) -> None:
    actor, auth = identity()
    auth = replace(auth, verified_at=None, status=AccountStatus.PENDING_VERIFICATION)
    data = replace(
        facts(operation, auth, obj=target(actor) if operation is Operation.ASK else None),
        requested_mode=ConversationMode.PUBLIC,
    )
    assert evaluate(actor, data).code is DenialCode.EMAIL_UNVERIFIED


def test_cooldown_only_applies_to_new_ask_and_includes_exact_boundary() -> None:
    actor, auth = identity()
    deadline = NOW + timedelta(hours=24)
    auth = replace(
        auth, ai_cooldown_until=deadline, idle_expires_at=deadline, absolute_expires_at=deadline
    )
    # 当前请求尚在有效会话内，未来边界用重新续期后的会话事实验证。
    request = facts(Operation.ASK, auth, obj=target(actor))
    denied = evaluate(actor, request)
    assert denied.code is DenialCode.AI_COOLDOWN and denied.retry_at == deadline
    renewed = replace(
        auth,
        idle_expires_at=deadline + timedelta(hours=1),
        absolute_expires_at=deadline + timedelta(days=1),
    )
    assert evaluate(actor, replace(request, now=deadline, authentication=renewed)).allowed
    resume = facts(Operation.RESUME_RUN, auth, obj=target(actor, TargetKind.RUN))
    assert resume.target is not None
    resume = replace(
        resume,
        context=ContextFacts(
            run_id=resume.target.object_id,
            mode=ConversationMode.PUBLIC,
            captured_scope_epoch=3,
            captured_generation=0,
            current_generation=0,
            sources=(),
            sources_complete=True,
        ),
    )
    assert evaluate(actor, resume).allowed
    assert evaluate(actor, facts(Operation.SEARCH_PUBLIC_KNOWLEDGE, auth)).allowed
    assert evaluate(actor, facts(Operation.CREATE_COMMENT, auth)).allowed


def test_public_ai_and_auto_never_grant_web_to_member() -> None:
    actor, auth = identity()
    request = facts(Operation.ASK, auth, obj=target(actor))
    assert evaluate(actor, replace(request, search_mode=SearchMode.AUTO)).allowed
    assert Capability.SEARCH_WEB not in capabilities_for(actor, request)
    assert (
        evaluate(actor, replace(request, search_mode=SearchMode.WEB)).code is DenialCode.FORBIDDEN
    )


def test_owner_conversation_creation_requires_step_up() -> None:
    actor, auth = identity(ActorRole.OWNER)
    data = replace(
        facts(Operation.CREATE_CONVERSATION, auth), requested_mode=ConversationMode.OWNER
    )
    assert evaluate(actor, data).code is DenialCode.STEP_UP_REQUIRED
    assert evaluate(actor, replace(data, requested_mode=ConversationMode.PUBLIC)).allowed


def test_selected_public_resource_is_scope_not_authorization() -> None:
    actor, auth = identity()
    owner, _ = identity(ActorRole.OWNER)
    res = resource(owner, published=False)
    data = replace(
        facts(Operation.SEARCH_PUBLIC_KNOWLEDGE, auth, res=res),
        requested_resource_id=res.resource_id,
    )
    assert evaluate(actor, data).code is DenialCode.NOT_FOUND
    assert evaluate(actor, replace(data, resource=None)).code is DenialCode.NOT_FOUND


def test_comment_creation_cannot_turn_missing_resource_into_guestbook() -> None:
    actor, auth = identity()
    request = facts(Operation.CREATE_COMMENT, auth)
    assert evaluate(actor, request).allowed
    assert (
        evaluate(actor, replace(request, requested_resource_id=uuid4())).code
        is DenialCode.NOT_FOUND
    )
    assert (
        evaluate(actor, replace(request, requested_parent_id=uuid4())).code is DenialCode.NOT_FOUND
    )


def test_reply_rejects_foreign_pending_parent_and_mismatched_attachment() -> None:
    actor, auth = identity()
    other, _ = identity()
    parent = target(other, TargetKind.COMMENT)
    request = facts(Operation.CREATE_COMMENT, auth, obj=parent)
    assert evaluate(actor, request).code is DenialCode.NOT_FOUND
    assert evaluate(actor, replace(request, target=replace(parent, comment_approved=True))).allowed
    assert evaluate(actor, replace(request, target=replace(parent, owner_id=actor.user_id))).allowed
    assert (
        evaluate(
            actor,
            replace(request, target=replace(parent, resource_id=uuid4(), comment_approved=True)),
        ).code
        is DenialCode.NOT_FOUND
    )


def test_comment_delete_allows_author_or_upgraded_moderator_but_edit_only_author() -> None:
    author, auth = identity()
    comment = target(author, TargetKind.COMMENT)
    assert evaluate(author, facts(Operation.DELETE_COMMENT, auth, obj=comment)).allowed
    owner, current = identity(ActorRole.OWNER, step_up=True)
    assert evaluate(owner, facts(Operation.DELETE_COMMENT, current, obj=comment)).allowed
    assert (
        evaluate(owner, facts(Operation.EDIT_COMMENT, current, obj=comment)).code
        is DenialCode.NOT_FOUND
    )
    other, session = identity()
    assert (
        evaluate(other, facts(Operation.DELETE_COMMENT, session, obj=comment)).code
        is DenialCode.NOT_FOUND
    )


@pytest.mark.parametrize(
    "operation",
    [
        Operation.READ_ACTION,
        Operation.EXECUTE_ACTION,
        Operation.CANCEL_ACTION,
        Operation.READ_MEMORY,
        Operation.UPDATE_MEMORY,
        Operation.DELETE_MEMORY,
    ],
)
def test_actions_and_memories_remain_personal_even_for_owner(operation: Operation) -> None:
    other, _ = identity(ActorRole.OWNER)
    actor, auth = identity(ActorRole.OWNER, step_up=True)
    kind = TargetKind.ACTION if "action" in operation.value else TargetKind.MEMORY
    assert (
        evaluate(actor, facts(operation, auth, obj=target(other, kind))).code
        is DenialCode.NOT_FOUND
    )
    assert evaluate(actor, facts(operation, auth, obj=target(actor, kind))).allowed


def test_expired_action_status_can_be_inspected_but_retained_memory_expiry_hides_content() -> None:
    actor, auth = identity(ActorRole.OWNER, step_up=True)
    action = replace(target(actor, TargetKind.ACTION), expires_at=NOW)
    assert evaluate(actor, facts(Operation.READ_ACTION, auth, obj=action)).allowed
    memory = replace(target(actor, TargetKind.MEMORY), expires_at=NOW)
    assert (
        evaluate(actor, facts(Operation.READ_MEMORY, auth, obj=memory)).code is DenialCode.NOT_FOUND
    )
