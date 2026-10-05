"""纯词表与 DB 词表的兼容性；ORM 只在此测试中引入，不进入权限核心。"""

import pytest

from autumn_backend.db import enums
from autumn_backend.policies.actor import ActorRole
from autumn_backend.policies.facts import AccountStatus, ConversationMode, SourceScope

pytestmark = pytest.mark.unit


def test_pure_vocabulary_matches_current_database_contract() -> None:
    assert {role.value for role in ActorRole if role is not ActorRole.ANONYMOUS} == {
        role.value for role in enums.UserRole
    }
    assert {status.value for status in AccountStatus} == {
        status.value for status in enums.UserStatus
    }
    assert {mode.value for mode in ConversationMode} == {
        mode.value for mode in enums.ConversationMode
    }
    assert {scope.value for scope in SourceScope} == {scope.value for scope in enums.IndexScope}


def test_invalidation_uses_frozen_sse_vocabulary() -> None:
    events = {event.value for event in enums.RunEventType}
    assert {"source.invalidated", "scope.changed", "error", "done"} <= events
    assert "run.invalidated" not in events
