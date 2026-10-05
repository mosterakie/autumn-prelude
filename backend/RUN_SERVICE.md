# E2 问答受理服务契约

`RunService.accept_run(actor, AcceptRunCommand)` 负责持久受理一次问答，返回不可变
`AskAccepted`，事务提交后才交还调用者。HTTP 路由、认证 Cookie、模型执行和 SSE
传输由 F/G/H 接入；返回的 `events_url` 是接口契约地址，当前还没有实现事件端点。

## 当前实现

锁顺序为 user → auth_session → conversation → 按 UUID 排序的选中资源 → quota_bucket
→ reservation。当前账号/会话记录、会话固定模式和选中资源一直持锁到提交，最后按
数据库实际时钟复核过期、升级验证和资源到期条件。后续恢复或禁用入口须遵守同一锁顺序。

1. 检查当前登录身份、邮箱验证、会话归属与模式、站长联网权限。
2. 按当前模式逐项验证 `resource_ids`。public 模式只允许未撤回且启用 AI 的公开投影；
   owner 模式要求已升级站长自己的活动资料。无权访问的资源或会话统一返回不存在。
3. 按 `(user_id, idempotency_key)` 读取原 Run，比对稳定请求摘要。原请求返回原 Run，
   不再使用新问答冷却/速率/并发/日额度规则，不创建输入、预留、事件或作业。
4. 新请求检查冷却截止、消息标识冲突、会话忙、账号执行名额、短期受理速率。
5. 创建 Run，取得上海时间当天的额度桶并预留一次；追加完整 user 消息，绑定输入指针。
6. 入队 `run.dispatch` 并追加一条 `run.status: queued` 事件，复核当前权限后一起提交。

任何一步失败都回滚 Run、额度桶/预留、速率计数、消息序号、消息和任务。
事务里只有数据库 I/O；模型、联网、邮件和对象存储均不在此调用。

## 请求身份与重试

摘要包含 schema_version、conversation_id、client_message_id、规范化消息、数据库中的
固定 mode、去重并排序后的 resource_ids、search_mode。只规范化 CRLF/CR 为 LF，
保留正文首尾空格。身份/配置变化不会改变已受理请求的摘要。

- 同 key 同摘要：返回原 run_id、input_message_id、当前持久 status。
- 同 key 不同摘要：`idempotency_conflict`。
- 已使用的 client_message_id 换新 key：`idempotency_conflict`，不会把旧消息绑定到新 Run。
- 当前权限仍须有效；原资源撤回/关闭 AI、登录失效、站长升级验证失效仍会拒绝重试。
- 重试只查询受理元数据，不恢复执行、不重绑定 auth_session、不返回旧来源正文。
  完整历史/来源依赖闭包、摘要失效与运行代际由 E6/G/H 承接。

输入上限为 32000 字符、最多 100 个资源 ID、幂等键 128 字符。
`AcceptRunCommand` 不接受客户端指定 user_id、mode、额度、授权或模型配置。

## AI 限额配置

`settings.key = ai_limits`，`schema_version = 1`。顶层四字段沿用前端普通用户配置形状；
可选 `owner` 子对象独立控制站长。未配置时使用下列明确默认值，配置存在但损坏时拒绝新受理。
站长默认值是首版有限开发配置，可通过后续设置服务调整。

```json
{
  "daily_limit": 10,
  "cooldown_hours": 24,
  "per_minute": 3,
  "concurrency": 1,
  "owner": {
    "daily_limit": 100,
    "cooldown_hours": 0,
    "per_minute": 10,
    "concurrency": 2
  }
}
```

四字段必须为非负整数，布尔值不算整数；上限依次为 1000000、8760、10000、100。
daily_limit/per_minute/concurrency 为 0 时禁止对应的新分配。省略 owner 时使用独立站长默认值，
不继承普通用户配置。未知字段或结构版本不被接受。默认策略 version 为 0；
已有配置读取 `Setting.version`，本节点只读取设置，修改入口留在 E7/F。

`cooldown_hours` 供 F 邮箱首次验证计算 `User.ai_cooldown_until`；E2 检查记录中的截止时间，
不在每次问答时重新计算，不因配置修改回溯改写已有用户。所有限制均来自服务端，system prompt 不负责限流。

账号执行名额只统计 queued/running/cancelling。waiting_input/waiting_approval/waiting_auth
不占账号执行名额，但继续占用当前 conversation；数据库非终态唯一索引保留最后仲裁。
恢复等待任务时 G/H 需要重新取得 user 锁并检查执行名额。

## 时间与额度返回

首版冻结 `Asia/Shanghai` 日窗口，以本地 00:00 切分，存储 UTC 起止时刻。
例如上海 2026-10-06 00:00 对应 UTC 2026-10-05 16:00。
使用 IANA ZoneInfo，并声明 tzdata 依赖供 Windows 环境使用；缺失数据或其它时区配置会明确失败。

每个已受理 Run 的 reservation 永远绑定原桶。跨午夜恢复/重试不会创建新桶或再次预留。
降低限额只影响后续新受理；Run.config_snapshot 保存实际采用的角色限额与配置版本。

`AskAccepted.quota` 描述这个 Run 的原桶：live used/reserved、受理时 daily_limit、
`remaining=max(0, daily_limit-used-reserved)`、原窗口结束时间、当前账号冷却截止和 DB server_time。
因此跨午夜重试仍可能返回过去的 next_reset_at；它不是“今天还能发起多少次”的查询接口。
F 的当前额度查询须按当前时间和当前配置另行装配。

速率为按 UTC 分钟对齐的 60 秒固定窗口，通过 PostgreSQL UPSERT 原子增加；
key 为服务端 HMAC(user_id) 和 ai.accept。失败事务不消耗速率，重试不进入该窗口。
固定窗口允许边界两侧短时突发；它是受理速率限制，登录/邮件防滥用由后续认证服务另行实施。
使用会话密钥进行域隔离 HMAC；轮换密钥会重置最长一分钟的速率身份，不改变每日额度。
过期速率桶清理由 H 承接，当前 expires_at 为窗口结束后一小时。

## 搜索与后续执行

普通用户显式 web 返回 403；auto 永不授予普通用户联网工具。
站长 web 要求当前与入口的升级验证均有效；auto 只有当时有效的已升级站长才记录 web_enabled。
这些快照描述受理决定，不是执行时永久授权。G/H 每次执行和输出仍须复核当前权限和来源。
public 模式会话和消息始终是本人的私人记录，公开资料范围不会公开聊天。

Run 快照只有模式、资源 ID、搜索策略、额度及版本，不保存密钥和正文。
Job 载荷只保存 run_id；初始事件只保存状态。完整正文在 Message，实际来源在后续 run_sources。

本节点不改变数据库结构。基础验收仅 4 项真实 PostgreSQL 流程，涵盖原请求重试、
当前权限/冷却与站长权限、账号等待/速率/额度边界、晚期入队失败整笔回滚；不新增并发测试矩阵。
