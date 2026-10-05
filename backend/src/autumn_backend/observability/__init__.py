"""observability 模块。

结构化日志（脱敏）、指标、Tracing、审计关联。
注意：审计不等于日志——真实审计写入 ``audit_events`` 表，由 repositories 层承担。

当前阶段：A1 提供日志初始化与脱敏 processor；指标与 Tracing 留给阶段 I2。
"""

from autumn_backend.observability.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
