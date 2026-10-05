from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from autumn_backend.policies.actor import ActorContext, ActorRole, Capability

pytestmark = pytest.mark.unit


def member() -> ActorContext:
    return ActorContext(
        user_id=uuid4(),
        role=ActorRole.MEMBER,
        auth_session_id=uuid4(),
        step_up_expires_at=None,
        capabilities=frozenset({Capability.READ_PUBLIC, Capability.OWN_CHAT}),
        scope_epoch=0,
    )


def test_actor_is_immutable_and_capabilities_cannot_be_mutated() -> None:
    actor = member()
    with pytest.raises(FrozenInstanceError):
        actor.role = ActorRole.OWNER  # type: ignore[misc]
    assert isinstance(actor.capabilities, frozenset)
    assert not hasattr(actor, "__dict__")


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": None},
        {"auth_session_id": None},
        {"user_id": "client-user"},
        {"role": "owner"},
        {"role": ActorRole.ANONYMOUS},
        {"capabilities": {Capability.SEARCH_WEB}},
        {"capabilities": frozenset({"search_web"})},
        {"scope_epoch": -1},
        {"scope_epoch": True},
        {"step_up_expires_at": datetime(2026, 10, 5)},
        {"step_up_expires_at": datetime(2026, 10, 5, tzinfo=UTC)},
    ],
)
def test_invalid_identity_snapshots_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(member(), **changes)


def test_anonymous_actor_and_owner_step_up_shapes() -> None:
    anonymous = ActorContext(
        user_id=None,
        role=ActorRole.ANONYMOUS,
        auth_session_id=None,
        step_up_expires_at=None,
        capabilities=frozenset({Capability.READ_PUBLIC}),
        scope_epoch=2,
    )
    assert anonymous.user_id is None
    owner = replace(
        member(), role=ActorRole.OWNER, step_up_expires_at=datetime(2026, 10, 5, tzinfo=UTC)
    )
    assert owner.auth_session_id is not None
