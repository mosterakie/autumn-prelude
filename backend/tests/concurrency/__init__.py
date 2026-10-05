"""tests.concurrency：真实 PostgreSQL 并发验收（阶段 C，硬闸门）。

五个用例（架构文档 §11.2 / Repository 文档 §16）：
1. 同一 idempotency_key 并发创建 Run
2. 两个不同 Run 并发首次创建当天 quota bucket
3. 两个 worker 抢同一 job，旧 lease 的 finish 被拒绝
4. 同一 run_id 并发 reserve 两次
5. 同一 run_id 并发 reserve 但 amount 不同

并发驱动要求：多协程 + 独立连接 + barrier 同步起跑。
"""
