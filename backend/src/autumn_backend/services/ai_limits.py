"""服务端 AI 限额配置与显式日窗口；不读取隐式时钟。"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autumn_backend.db.models import Setting
from autumn_backend.errors import ConfigurationError
from autumn_backend.policies import ActorRole


@dataclass(frozen=True, slots=True, kw_only=True)
class RoleLimits:
    daily_limit: int = 10
    cooldown_hours: int = 24
    per_minute: int = 3
    concurrency: int = 1

    def snapshot(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class AiLimits:
    version: int = 0
    member: RoleLimits = RoleLimits()
    owner: RoleLimits = RoleLimits(daily_limit=100, cooldown_hours=0, per_minute=10, concurrency=2)

    def for_role(self, role: ActorRole) -> RoleLimits:
        if role is ActorRole.OWNER:
            return self.owner
        if role is ActorRole.MEMBER:
            return self.member
        raise ConfigurationError("匿名身份没有 AI 额度策略")


_FIELDS = frozenset({"daily_limit", "cooldown_hours", "per_minute", "concurrency"})
_MAXIMUMS = {
    "daily_limit": 1_000_000,
    "cooldown_hours": 8760,
    "per_minute": 10000,
    "concurrency": 100,
}


def _role_limits(value: object) -> RoleLimits:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ConfigurationError("AI 限额结构无效")
    for name in _FIELDS:
        number = value[name]
        if type(number) is not int or not 0 <= number <= _MAXIMUMS[name]:
            raise ConfigurationError("AI 限额数值无效")
    return RoleLimits(**value)


def read_ai_limits(setting: Setting | None) -> AiLimits:
    """未配置时使用明确默认值；已配置但损坏时拒绝，不能偷偷放宽限制。

    member 四字段保持前端现有结构，owner 子对象可选且独立；v0 为代码默认。
    """
    if setting is None:
        return AiLimits()
    value = setting.value
    if (
        setting.schema_version != 1
        or type(setting.version) is not int
        or setting.version < 0
        or not isinstance(value, dict)
        or set(value) not in (_FIELDS, _FIELDS | {"owner"})
    ):
        raise ConfigurationError("AI 限额配置版本或结构无效")
    member = _role_limits({name: value[name] for name in _FIELDS})
    owner = _role_limits(value["owner"]) if "owner" in value else AiLimits().owner
    return AiLimits(version=setting.version, member=member, owner=owner)


def limits_from_snapshot(value: Any) -> RoleLimits:
    return _role_limits(value)


def daily_window(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ConfigurationError("额度时钟必须带时区")
    # 首版冻结窗口定义，避免修改时区后同一账号同时使用两套日额度桶。
    if timezone != "Asia/Shanghai":
        raise ConfigurationError("首版日额度时区固定为 Asia/Shanghai")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ConfigurationError("额度时区数据不可用") from error
    day = now.astimezone(zone).date()
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return start.astimezone(UTC), end.astimezone(UTC)
