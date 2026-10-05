"""不同表族不会从基类继承不合适的通用写接口。"""

import pytest

from autumn_backend.repositories.base import (
    AppendOnlyRepository,
    ControlledMutableRepository,
    RepositoryBase,
    UUIDRepository,
)

pytestmark = pytest.mark.unit


def test_restricted_capabilities() -> None:
    for repository in (RepositoryBase, AppendOnlyRepository, ControlledMutableRepository):
        for name in ("record", "create", "update", "delete", "commit", "rollback"):
            assert not hasattr(repository, name)
    assert not hasattr(RepositoryBase, "get")
    assert hasattr(UUIDRepository, "get_for_update")
