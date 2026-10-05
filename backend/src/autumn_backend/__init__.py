"""秋序（Autumn Prelude）后端与 Agent。

模块化单体：HTTP API、Agent Runtime、Worker 是三个入口，共享 services、
repositories、policies 与基础设施适配器。

依赖方向（架构文档 §1.2，硬约束）::

    api               -> services / auth / observability
    agent.runtime     -> services / policies / providers（经工具适配）
    workers.handlers  -> services / jobs.queue / agent.runtime
    services          -> policies / repositories / providers / storage / jobs.queue
    repositories      -> db / observability
    jobs.queue        -> 数据库队列基础设施，不认识业务 handler

明确允许 ``workers -> agent.runtime``；
明确禁止 ``agent -> workers``、``services -> agent``、``services -> workers``。
阶段 I1 由 import-linter 把这三条禁止依赖锁进 CI。
"""

__version__ = "0.1.0"
