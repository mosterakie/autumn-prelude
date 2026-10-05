from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from autumn_backend.policies.actor import ActorRole, Capability
from autumn_backend.policies.decision import DenialCode
from autumn_backend.policies.facts import AccountStatus, ConversationMode, Operation, TargetKind
from autumn_backend.policies.policy import evaluate
from tests.unit.policy_helpers import NOW, anonymous, facts, identity, resource, target

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("operation", [Operation.READ_CONVERSATION, Operation.READ_RUN])
@pytest.mark.parametrize("role", list(ActorRole))
def test_foreign_chat_and_absent_chat_are_identical(operation: Operation, role: ActorRole) -> None:
    other, _ = identity()
    actor, auth = (
        identity(role, step_up=role is ActorRole.OWNER)
        if role is not ActorRole.ANONYMOUS
        else (anonymous(), None)
    )
    kind = TargetKind.RUN if operation is Operation.READ_RUN else TargetKind.CONVERSATION
    missing = evaluate(actor, facts(operation, auth))
    foreign = evaluate(actor, facts(operation, auth, obj=target(other, kind)))
    assert missing == foreign
    assert foreign.code is DenialCode.NOT_FOUND
    assert foreign.http_status == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": uuid4()},
        {"session_id": uuid4()},
        {"session_user_id": uuid4()},
        {"role": ActorRole.OWNER},
        {"status": AccountStatus.DISABLED},
        {"user_auth_version": 2},
        {"revoked_at": NOW},
        {"user_deleted_at": NOW},
        {"idle_expires_at": NOW},
        {"idle_expires_at": NOW, "absolute_expires_at": NOW},
    ],
)
def test_own_chat_rechecks_current_session_and_user(changes: dict[str, object]) -> None:
    actor, auth = identity()
    current = replace(auth, **changes)
    result = evaluate(actor, facts(Operation.READ_CONVERSATION, current, obj=target(actor)))
    assert result.code is DenialCode.SESSION_EXPIRED
    assert result.http_status == 401


def test_own_public_chat_does_not_require_step_up_or_new_email_verification() -> None:
    actor, auth = identity(ActorRole.OWNER)
    auth = replace(auth, status=AccountStatus.PENDING_VERIFICATION, verified_at=None)
    assert evaluate(actor, facts(Operation.READ_CONVERSATION, auth, obj=target(actor))).allowed


@pytest.mark.parametrize("expires", [None, NOW - timedelta(seconds=1), NOW])
def test_owner_mode_rechecks_both_step_up_snapshots(expires: object) -> None:
    actor, auth = identity(ActorRole.OWNER, step_up=True)
    private = target(actor, mode=ConversationMode.OWNER)
    stale_actor = replace(actor, step_up_expires_at=expires)
    stale_auth = replace(auth, step_up_expires_at=expires)
    for principal, current in [(stale_actor, auth), (actor, stale_auth)]:
        result = evaluate(principal, facts(Operation.READ_CONVERSATION, current, obj=private))
        assert result.code is DenialCode.STEP_UP_REQUIRED


def test_requested_mode_and_echoed_capabilities_cannot_override_private_chat() -> None:
    actor, auth = identity(ActorRole.OWNER)
    actor = replace(actor, capabilities=frozenset(Capability))
    private = target(actor, mode=ConversationMode.OWNER)
    data = replace(
        facts(Operation.READ_CONVERSATION, auth, obj=private),
        requested_mode=ConversationMode.PUBLIC,
    )
    assert evaluate(actor, data).code is DenialCode.STEP_UP_REQUIRED


@pytest.mark.parametrize(
    "changes",
    [
        {"publication": None},
        {"is_deleted": True},
        {"is_archived": True},
        {"expires_at": NOW},
    ],
)
def test_unavailable_public_resources_are_not_found(changes: dict[str, object]) -> None:
    owner, _ = identity(ActorRole.OWNER)
    current = replace(resource(owner), **changes)
    assert (
        evaluate(anonymous(), facts(Operation.READ_PUBLIC_RESOURCE, res=current)).code
        is DenialCode.NOT_FOUND
    )


@pytest.mark.parametrize("revoked", [True, False])
def test_historical_or_revoked_publication_cannot_be_read(revoked: bool) -> None:
    owner, _ = identity(ActorRole.OWNER)
    res = resource(owner)
    assert res.publication is not None
    publication = (
        replace(res.publication, revoked_at=NOW)
        if revoked
        else replace(res.publication, is_current=False)
    )
    assert (
        evaluate(
            anonymous(),
            facts(Operation.READ_PUBLIC_RESOURCE, res=replace(res, publication=publication)),
        ).code
        is DenialCode.NOT_FOUND
    )


def test_public_read_is_anonymous_and_file_requires_its_own_switch() -> None:
    owner, auth = identity(ActorRole.OWNER)
    res = resource(owner)
    assert evaluate(anonymous(), facts(Operation.READ_PUBLIC_RESOURCE, res=res)).allowed
    assert evaluate(
        owner,
        facts(
            Operation.READ_PUBLIC_RESOURCE, replace(auth, status=AccountStatus.DISABLED), res=res
        ),
    ).allowed
    assert (
        evaluate(anonymous(), facts(Operation.READ_PUBLIC_FILE, res=res)).code
        is DenialCode.NOT_FOUND
    )
    assert res.publication is not None
    res = replace(
        res, publication=replace(res.publication, raw_download_enabled=True, ai_enabled=False)
    )
    assert evaluate(anonymous(), facts(Operation.READ_PUBLIC_FILE, res=res)).allowed


def test_updating_private_revision_does_not_remove_existing_public_projection() -> None:
    owner, _ = identity(ActorRole.OWNER)
    res = replace(resource(owner), current_revision_id=uuid4())
    assert evaluate(anonymous(), facts(Operation.READ_PUBLIC_RESOURCE, res=res)).allowed


@pytest.mark.parametrize("approved", [False, True])
def test_public_comments_require_approval_and_current_related_resource(approved: bool) -> None:
    author, _ = identity()
    owner, _ = identity(ActorRole.OWNER)
    comment = replace(target(author, TargetKind.COMMENT), comment_approved=approved)
    assert (
        evaluate(anonymous(), facts(Operation.READ_PUBLIC_COMMENT, obj=comment)).allowed is approved
    )
    res = resource(owner, published=False)
    comment = replace(comment, resource_id=res.resource_id, comment_approved=True)
    assert (
        evaluate(anonymous(), facts(Operation.READ_PUBLIC_COMMENT, obj=comment, res=res)).code
        is DenialCode.NOT_FOUND
    )
    assert (
        evaluate(anonymous(), facts(Operation.READ_PUBLIC_COMMENT, obj=comment)).code
        is DenialCode.NOT_FOUND
    )


def test_private_resources_require_owner_ownership_and_step_up() -> None:
    owner, auth = identity(ActorRole.OWNER)
    res = resource(owner, published=False)
    assert (
        evaluate(owner, facts(Operation.READ_PRIVATE_RESOURCE, auth, res=res)).code
        is DenialCode.STEP_UP_REQUIRED
    )
    actor, current = identity(ActorRole.OWNER, step_up=True)
    assert (
        evaluate(actor, facts(Operation.READ_PRIVATE_RESOURCE, current, res=res)).code
        is DenialCode.NOT_FOUND
    )
    assert evaluate(
        actor, facts(Operation.READ_PRIVATE_RESOURCE, current, res=resource(actor, published=False))
    ).allowed


def test_citation_requires_run_owner_and_current_ai_permission() -> None:
    actor, auth = identity()
    owner, _ = identity(ActorRole.OWNER)
    res = resource(owner)
    citation = replace(target(actor, TargetKind.CITATION), resource_id=res.resource_id)
    assert evaluate(actor, facts(Operation.READ_CITATION, auth, obj=citation, res=res)).allowed
    assert res.publication is not None
    res = replace(res, publication=replace(res.publication, ai_enabled=False))
    assert (
        evaluate(actor, facts(Operation.READ_CITATION, auth, obj=citation, res=res)).code
        is DenialCode.NOT_FOUND
    )
    assert (
        evaluate(owner, facts(Operation.READ_CITATION, None, obj=citation, res=res)).code
        is DenialCode.NOT_FOUND
    )


def test_unknown_or_incomplete_operations_fail_closed() -> None:
    actor, auth = identity(ActorRole.OWNER, step_up=True)
    assert not evaluate(actor, facts(Operation.READ_CITATION, auth)).allowed
    assert not evaluate(actor, facts(Operation.EXECUTE_ACTION, auth)).allowed
