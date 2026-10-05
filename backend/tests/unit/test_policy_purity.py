"""在没有第三方包的解释器中验证核心可导入，判定期间禁止文件和网络访问。"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
POLICY_PATH = Path(__file__).resolve().parents[2] / "src" / "autumn_backend" / "policies"


def test_policy_core_needs_only_standard_library_and_performs_no_io() -> None:
    source_path = POLICY_PATH.parent.parent
    script = f"""
import sys
sys.path.insert(0, {str(source_path)!r})
from datetime import UTC, datetime, timedelta
from uuid import UUID
from autumn_backend.policies import ActorContext, ActorRole, capabilities_for, evaluate
from autumn_backend.policies.facts import AccountStatus, AuthenticationFacts, Operation, PolicyFacts

assert not {{'sqlalchemy', 'fastapi', 'pydantic', 'asyncpg'}} & set(sys.modules)
now = datetime(2026, 10, 5, tzinfo=UTC)
user_id, session_id = UUID(int=1), UUID(int=2)
actor = ActorContext(user_id=user_id, role=ActorRole.OWNER, auth_session_id=session_id,
    step_up_expires_at=now + timedelta(minutes=15), capabilities=frozenset(), scope_epoch=0)
auth = AuthenticationFacts(user_id=user_id, session_id=session_id, session_user_id=user_id,
    role=ActorRole.OWNER, status=AccountStatus.ACTIVE, user_auth_version=1, session_auth_version=1,
    verified_at=now, idle_expires_at=now + timedelta(hours=1), absolute_expires_at=now + timedelta(days=1),
    step_up_expires_at=now + timedelta(minutes=15))
cases = [PolicyFacts(operation=operation, now=now, current_scope_epoch=0, authentication=auth)
    for operation in Operation]

def no_io(event, args):
    if event in {{'open', 'os.listdir', 'os.scandir', 'os.system', 'subprocess.Popen'}} or event.startswith('socket.'):
        raise AssertionError('policy performed I/O: ' + event)

sys.addaudithook(no_io)
for facts in cases:
    first = evaluate(actor, facts)
    assert first == evaluate(actor, facts)
    assert isinstance(first.allowed, bool)
    assert isinstance(capabilities_for(actor, facts), frozenset)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_policy_core_does_not_read_implicit_clock_or_environment() -> None:
    forbidden = {"now", "utcnow", "today", "time", "monotonic", "perf_counter", "getenv"}
    for path in POLICY_PATH.glob("*.py"):
        if path.name == "loader.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden, (
                    f"implicit input in {path.name}:{node.lineno}"
                )
