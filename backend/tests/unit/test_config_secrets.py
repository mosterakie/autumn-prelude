"""A-P1-3 回归：生产配置必须拒绝开发默认密钥，并在错误信息里不泄露密钥。

现有校验只检查长度，而代码里的开发默认值本身就超过 32 字符，
因此"未提供任何密钥 + prod"也会构造成功——这直接违背 A1 宣称的 fail-closed。
"""

from __future__ import annotations

import pytest

from autumn_backend.config import (
    _DEV_CSRF_SECRET,
    _DEV_SESSION_SECRET,
    Environment,
    Settings,
)

pytestmark = pytest.mark.unit

_VALID_SESSION = "s" * 40
_VALID_CSRF = "c" * 40

_PROD_BASE: dict[str, object] = {
    "environment": Environment.PROD,
    "cookie_secure": True,
    "cookie_name": "__Host-autumn_session",
}


def _prod(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_PROD_BASE, **overrides})  # type: ignore[call-arg]


class TestProductionRejectsMissingSecrets:
    def test_both_missing_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="AUTUMN_SESSION_SECRET"):
            _prod()

    def test_only_session_missing_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="AUTUMN_SESSION_SECRET"):
            _prod(csrf_secret=_VALID_CSRF)

    def test_only_csrf_missing_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="AUTUMN_CSRF_SECRET"):
            _prod(session_secret=_VALID_SESSION)


class TestProductionRejectsDevPlaceholders:
    def test_dev_session_placeholder_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="AUTUMN_SESSION_SECRET"):
            _prod(session_secret=_DEV_SESSION_SECRET, csrf_secret=_VALID_CSRF)

    def test_dev_csrf_placeholder_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="AUTUMN_CSRF_SECRET"):
            _prod(session_secret=_VALID_SESSION, csrf_secret=_DEV_CSRF_SECRET)

    def test_too_short_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="AUTUMN_SESSION_SECRET"):
            _prod(session_secret="short", csrf_secret=_VALID_CSRF)


class TestProductionAcceptsExplicitSecrets:
    def test_valid_explicit_secrets_are_accepted(self) -> None:
        settings = _prod(session_secret=_VALID_SESSION, csrf_secret=_VALID_CSRF)
        assert settings.is_production
        assert settings.session_secret.get_secret_value() == _VALID_SESSION

    def test_error_message_does_not_leak_secret_values(self) -> None:
        """校验失败时不得把密钥内容写进异常文本（可能被日志收走）。"""
        secret = "LEAK-CANARY-0123456789-LEAK-CANARY"
        with pytest.raises(ValueError) as excinfo:
            _prod(session_secret=secret, csrf_secret="short")
        assert secret not in str(excinfo.value)

    def test_dev_placeholders_still_work_in_non_production(self) -> None:
        """非生产环境仍可用占位密钥，本地开发不应被迫配置密钥。"""
        settings = Settings(_env_file=None, environment=Environment.LOCAL)  # type: ignore[call-arg]
        assert settings.session_secret.get_secret_value() == _DEV_SESSION_SECRET

    def test_test_environment_uses_its_own_defaults(self) -> None:
        settings = Settings(_env_file=None, environment=Environment.TEST)  # type: ignore[call-arg]
        assert not settings.is_production


class TestSourceFilesAreNotGitIgnored:
    """根 .gitignore 的 ``storage/`` 曾误吞 Python 包，导致 storage 包没进提交。

    这类缺陷本地测试查不出来（文件确实存在），只能在"是否被版本控制跟踪"这一层发现。
    """

    def test_no_source_file_under_src_is_git_ignored(self) -> None:
        import subprocess
        from pathlib import Path

        backend_root = Path(__file__).resolve().parents[2]
        src_root = backend_root / "src"

        try:
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=backend_root,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("未在 git 工作树中，跳过版本控制跟踪检查")

        files = [str(path.relative_to(backend_root)) for path in src_root.rglob("*.py")]
        assert files, "src 下应当有 Python 源文件"

        ignored: list[str] = []
        for relative in files:
            result = subprocess.run(
                ["git", "check-ignore", "-q", relative],
                cwd=backend_root,
                capture_output=True,
            )
            # 退出码 0 = 被忽略，1 = 未被忽略，其余为错误。
            if result.returncode == 0:
                ignored.append(relative)

        assert not ignored, f"以下源文件被 .gitignore 忽略，不会进入提交：{ignored}"

    def test_expected_package_layout_exists(self) -> None:
        """A1 清单里的模块包必须都存在且可导入（storage 曾漏提交）。"""
        from pathlib import Path

        backend_root = Path(__file__).resolve().parents[2]
        package_root = backend_root / "src" / "autumn_backend"

        for name in (
            "api",
            "auth",
            "policies",
            "services",
            "agent",
            "repositories",
            "jobs",
            "workers",
            "providers",
            "storage",
            "observability",
            "db",
        ):
            module_dir = package_root / name
            assert (module_dir / "__init__.py").is_file(), f"缺少模块包：{name}"
