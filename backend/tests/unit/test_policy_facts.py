from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from autumn_backend.policies.actor import ActorRole
from autumn_backend.policies.facts import (
    AccountStatus,
    AuthenticationFacts,
    ConversationMode,
    Operation,
    PolicyFacts,
    PublicationFacts,
    ResourceFacts,
    TargetFacts,
    TargetKind,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 5, tzinfo=UTC)


def auth() -> AuthenticationFacts:
    user_id = uuid4()
    return AuthenticationFacts(
        user_id=user_id,
        session_id=uuid4(),
        session_user_id=user_id,
        role=ActorRole.MEMBER,
        status=AccountStatus.ACTIVE,
        user_auth_version=1,
        session_auth_version=1,
        verified_at=NOW,
        idle_expires_at=NOW + timedelta(hours=1),
        absolute_expires_at=NOW + timedelta(days=1),
    )


def test_nested_facts_are_frozen() -> None:
    facts = PolicyFacts(
        operation=Operation.ASK, now=NOW, current_scope_epoch=0, authentication=auth()
    )
    with pytest.raises(FrozenInstanceError):
        facts.now = NOW + timedelta(hours=1)  # type: ignore[misc]
    assert facts.authentication is not None
    with pytest.raises(FrozenInstanceError):
        facts.authentication.role = ActorRole.OWNER  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"now": datetime(2026, 10, 5)},
        {"current_scope_epoch": True},
        {"current_scope_epoch": -1},
        {"operation": "ask"},
        {"authentication": {"role": "owner"}},
        {"target": object()},
        {"search_mode": "web"},
    ],
)
def test_facts_reject_untyped_or_mutable_input(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(PolicyFacts(operation=Operation.ASK, now=NOW, current_scope_epoch=0), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"role": ActorRole.ANONYMOUS},
        {"status": "active"},
        {"user_auth_version": 0},
        {"session_auth_version": True},
        {"idle_expires_at": NOW + timedelta(days=2)},
        {"step_up_expires_at": datetime(2026, 10, 5)},
    ],
)
def test_authentication_fact_shape_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(auth(), **changes)


def test_cross_resource_publication_cannot_be_assembled() -> None:
    publication = PublicationFacts(
        publication_id=uuid4(),
        resource_id=uuid4(),
        revision_id=uuid4(),
        is_current=True,
        ai_enabled=True,
        raw_download_enabled=False,
    )
    with pytest.raises(ValueError, match="belong"):
        ResourceFacts(
            resource_id=uuid4(),
            owner_id=uuid4(),
            current_revision_id=uuid4(),
            acl_version=0,
            publication=publication,
        )


def test_chat_targets_require_fixed_mode_and_owner() -> None:
    with pytest.raises(ValueError, match="fixed mode"):
        TargetFacts(object_id=uuid4(), kind=TargetKind.RUN, owner_id=uuid4())
    with pytest.raises(ValueError, match="fixed mode"):
        TargetFacts(
            object_id=uuid4(), kind=TargetKind.RUN, owner_id=None, mode=ConversationMode.PUBLIC
        )


def test_missing_objects_are_explicit_and_do_not_load_anything() -> None:
    facts = PolicyFacts(operation=Operation.READ_PRIVATE_RESOURCE, now=NOW, current_scope_epoch=7)
    assert facts.resource is None
    assert facts.target is None
