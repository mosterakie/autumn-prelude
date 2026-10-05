# E3 留言提交服务契约

`CommentService.create_comment(actor, CreateCommentCommand)` 对接 B8 的 `create_or_get`，
把当前权限、提交身份、一级回复和审计放进同一短事务。成功返回不可变 `CommentDTO`，
退出 UoW 提交成功后才交还调用者。本节点没有 HTTP 路由或公开列表端点。

## 输入与结果

输入仅包含 UUID client_id、正文 body、可选 resource_id 与 parent_id。作者由当前服务端
ActorContext 和数据库身份记录确定；客户端不能指定作者、角色或审核状态。
正文不得为空白，最多 2000 字符，与当前前端输入上限一致。CRLF/CR 规范化为 LF，
首尾空白保留，不把不同 Unicode 字符替换为相同字符。

resource_id 为空表示留言板；不为空时要求资源当前公开、未删除、未归档且未到期。
无需启用资料 AI 检索，也不要求用户结束 AI 冷却。用户必须当前登录有效且邮箱已验证。
站长以普通留言能力提交，不需要升级验证，也不会自动通过审核。

新留言使用数据库默认 pending。CommentDTO 包含 id、resource_id、parent_id、
author_display_name、body、status、created_at、updated_at、version。
未设置显示名称时返回“秋序访客”，不从邮箱构造名字，不返回邮箱、密码或登录会话字段。
这是本人提交结果：pending 不代表内容已经公开。

## 事务与重试

锁顺序为 user → auth_session → resource（如有）→ 原提交结果或父留言。
同账号提交由 user 行锁串行化；原提交查询强制 author_id/client_id 范围，最后仍由
`UNIQUE(author_id, client_id)` 和定向 PostgreSQL UPSERT 仲裁。
资源、身份与父留言持锁到提交；提交前按数据库实际时间复核身份与资源到期条件。

1. 校验当前登录/邮箱，再校验当前文章公开范围。
2. 按 author_id/client_id 查询原结果；已删除结果返回不存在，不恢复、不新增。
3. 仅新回复查父留言：必须当前可见且同一 resource_id，留言板的 NULL 范围也明确比对。
4. 调用 B8 `create_or_get`，同提交身份不同原始摘要返回幂等冲突。
5. 只有首次创建追加 `comment.create` 审计。审计失败时留言一起回滚。
6. 返回当前本人结果，最后复核当前权限，再一起提交。

摘要沿用 B8，包含 resource_id、parent_id、规范化的原始 body。
之后编辑正文或审核不改写原始 request_hash：原请求重试返回同一留言的当前正文、状态和版本，
不把旧正文重新覆盖回来，不重复审计。相同 client_id 可分别用于不同账号，不共享结果。

已创建回复的重试不重新应用“现在还能创建回复”的父留言条件；父留言之后被删除，不会使
本人已存在回复的查询变成一次新建。留言本身已删除仍为 404，文章当前撤回也为 404。
这些语义只适用于本人提交结果，不授予任何公开读取或审核权限。

## 404 / 409 边界

| 情况 | 领域结果 | 后续 HTTP 映射 |
| --- | --- | --- |
| 匿名或当前登录失效 | AuthorizationError 的 AUTH_REQUIRED / SESSION_EXPIRED 判定 | 401 |
| 邮箱未验证 | AuthorizationError 的 EMAIL_UNVERIFIED 判定 | 403 |
| 私人、撤回、归档、删除、到期或缺失的文章 | NotFoundError | 404 NOT_FOUND |
| 缺失/已删除父留言、其他用户未审核父留言、父子资源不一致 | NotFoundError | 404 NOT_FOUND |
| 已删除的本人原提交结果 | NotFoundError | 404 NOT_FOUND |
| 同 author/client_id，但请求语义不同 | IdempotencyConflictError（ConflictError 子类） | 409 IDEMPOTENCY_CONFLICT |
| 可见父留言本身已经是回复 | B8 ConflictError | 409 |
| UUID、空白或超长正文无效 | InvalidInputError | 400 |

先检查父留言可见性，再检查一级回复结构；不能通过不同的错误枚举不可见父留言层级。
真正的 HTTP 异常封装、CSRF 与统一 request_id 由 F 实现，本节点只提供领域错误。

## 审计与后续边界

审计只保存操作者、resource_id、comment_id、pending 状态和请求标识，不复制正文、邮箱、
父留言文本或令牌。AuditEvent.before_version/after_version 专指资源版本，因此创建留言时为空。
comment_id 是 AuditMetadata 新增的可选身份字段，不需要修改数据库结构。
服务只执行数据库 I/O，没有模型、搜索、邮件或对象存储调用，也没有 AI 配额或 Job 副作用。

公开列表仍须过滤 approved、未删除，以及关联文章当前公开；公开 DTO 不含邮箱。
正文编辑需 expected_version，已审核内容须重新进入 pending。删除、举报、审核管理及其 HTTP
入口依照 F 后续节点接入，不把本节点的提交结果当成公开列表或审核实现。

## 基础验收状态（2026-10-06）

- 新增 4 项真实 PostgreSQL 基础用例：提交/重试、当前身份与文章权限、回复结构/删除边界、审计故障回滚。
- 4 项全部通过，0 跳过；未运行全量回归或扩展并发矩阵。
- Ruff、修改文件格式、64 源文件 mypy strict 与 git diff --check 通过；没有增加 schema 迁移或依赖。
- 使用 PostgreSQL 5442 的独立测试库；数据在外层事务回滚，没有修改业务库。
- 本节点本地提交，不推送；HTTP、公开列表、编辑/删除/举报与审核管理仍按 F 后续节点实现。
