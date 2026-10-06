# F 阶段 API 交付与接入说明

2026-10-06，F1–F8 完成。API 只解析请求、构造服务端身份、调用服务和编码响应；不直接访问 ORM 或供应商。所有业务事务通过独立短 UoW 完成，发送 SSE 和读取对象文件发生在事务之外。

## 已接入的入口

所有路径以 `/api` 开头；成功 JSON 为 `{data, request_id}`，删除成功为 204。日期输出 UTC `Z`，领域错误使用稳定 code；无效请求模型返回 422，服务语义输入错误按统一映射返回 400。不存在、私人或已撤回公开对象统一 404。

| 入口                                                                             | 行为与权限                                                      |
| -------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `/auth/register`、`/auth/verify-email`、`/auth/resend-verification`              | 邮箱密码注册、一次性验证和重发；公开注册只能产生 member         |
| `/auth/login`、`/auth/logout`、`/auth/me`                                        | Cookie 登录、登出、本人身份和当前 CSRF；匿名 me 返回匿名结果    |
| `/auth/forgot-password`、`/auth/reset-password`、`/auth/step-up`                 | 重置与站长 TOTP/恢复码验证；会话失效或轮换按认证契约处理        |
| `GET /me/quota`                                                                  | 本人今日额度、预占、冷却、重置时间；不允许指定其他账号          |
| `GET /public/site`                                                               | 固定安全站点信息、导航与角色方向，不返回私人设置                |
| `GET /public/notes`、`/public/notes/{slug}`、`/public/bookmarks`                 | 当前公开投影；列表支持游标、limit 和标签                        |
| `GET /public/sources/{publication_id}`、`/public/sources/{publication_id}/file`  | 当前来源与受控下载；下载前后重新检查公开范围                    |
| `/resources`、`/resources/{id}`                                                  | 已升级站长的文章/收藏创建、列表、读取、版本化编辑、双版本软删除 |
| `GET /resources/{id}/versions`、`/resources/{id}/versions/{revision_id}/file`    | 本人资料的版本页与确切版本文件；不返回存储 key                  |
| `POST /resources/{id}/publication/preview`、`/resources/{id}/publication/revoke` | 发布进入固定预览，页面明确撤回直接走事务服务                    |
| `/conversations`、`/conversations/{id}`、`GET /conversations/{id}/messages`      | 本人固定 public/owner 模式会话、分页、改名、软删除与消息历史    |
| `POST /ask`、`GET /runs/{id}`                                                    | 幂等受理并预占额度/入队，读取本人 Run；联网仅已升级站长         |
| `POST /runs/{id}/cancel`、`/runs/{id}/resume`                                    | 取消执行资格；恢复输入等待或认证等待，不重新受理或计次          |
| `GET /runs/{id}/events`、`/citations/{id}`                                       | 当前授权的事件与来源；站长也不能读其他账号聊天                  |
| `GET /public/comments`、`POST /comments`、`PATCH/DELETE /comments/{id}`          | 公开已审核留言、验证用户提交、作者编辑、作者或已升级站长删除    |
| `POST /reports`                                                                  | 已验证用户举报当前公开可见留言，同账号/留言的 open 举报去重     |
| `/moderation/comments`、`/moderation/comments/{id}/decision`                     | 已升级站长读取审核页，approve/reject/hide，版本冲突拒绝覆盖     |
| `/moderation/reports`、`/moderation/reports/{id}/resolve`                        | 已升级站长读取和处理举报                                        |
| `GET /actions/{id}`、`POST /actions/{id}/execute`、`/actions/{id}/cancel`        | 动作本人且当前站长验证有效；只能确认或取消固定动作              |

## 身份、版本与重试

- 每个已认证 POST/PATCH/DELETE 同时检查可信 Origin 和当前 Cookie 的 `X-CSRF-Token`，先于业务和请求体处理。匿名认证写请求同样检查 Origin。生产 Cookie 和密钥要求见 [认证说明](AUTH_CONTRACT.md)。
- 资源修改使用目标 `expected_version`，涉及公开范围同时使用 `expected_acl_version`；创建/编辑/删除和发布预览使用 `Idempotency-Key`。同键不同语义为 409，重试不覆盖原稿或重复排队。
- 留言使用 `client_id` 去重。编辑、删除、审核和举报处理使用目标 `expected_version`；DELETE 留言的 JSON 体为 `{expected_version}`。编辑正文总是重新进入 pending。
- ActionDTO 的 `version` 是动作自身版本，`expected_version` 是预览的目标版本，二者不能混用。execute 体为 `{expected_action_version: action.version, parameters_hash}`，cancel 体为 `{expected_action_version: action.version}`；不接受替换目标或参数。
- execute 返回 202 和实际动作状态。初次确认是 ready 并持久入队；不代表已经发布。已成功动作返回既有成功结果；已取消或失败动作不能再次确认。取消只适用于尚未开始执行的动作。
- ActionDTO 的 summary、changes、impact 由固定服务端命令投影产生；result 使用白名单。当前 `can_undo=false`，没有开放补偿撤销入口。
- 留言和举报 DTO 不含邮箱或账号角色。审计只保存对象身份、受控状态、字段名和版本；自由输入的审核 reason 仅用于请求校验，不复制进脱敏审计。留言/动作版本放在 metadata，审计 before/after_version 仍专指资源版本。

## 公开读取与 SSE

公开数据由 publication 白名单字段构建；私人原稿不会作为缺失字段的回退。公开留言要求自己及父留言已审核、未删除，关联资料当前仍公开。撤回、隐藏、删除或到期阻断后续读取，所有 `/api` 响应 no-store。

SSE 是累计快照，不拼接历史增量。每次连接先返回当前状态和消息，再读取 after 后的事件；前端按 content_version 替换。Last-Event-ID 与 after 并存必须一致，负值或超出当前序号范围拒绝。旧代际消息事件不重放，run_events 中的正文不作为输出来源。

每批最多 100 条，在短事务中重新读取 User/Session、mode、step-up 和完整当前来源闭包。发送期间不持有 UoW；每秒轮询，15 秒无数据心跳。权限失效发送脱敏失效提示并结束，携带本连接已发消息 ID 供界面隐藏；已发字节本身不能收回。

等待输入/审批/验证与终态完成回放后关闭连接；连接断开不修改 Run，不取消、不重新计次。前端等待时停止自动重连，恢复或确认后重新读取同一任务。真正的取消通过 cancel 入口执行。

## 本阶段边界

F 阶段交付后，LangGraph、Worker、业务 handler、DeepSeek/Tavily/百炼及 SMTP 适配已在后续节点接入，见 [项目归档](../docs/archive/README.md)。认证邮件加密持久入队，HTTP 受理成功不代表邮件已发出或模型已回答；清理入队也不代表对象已物理清理。

知识上传/网页刷新、通用任务查询/取消/重试、设置/记忆/审计管理 HTTP 和补偿撤销仍是整体接口契约中的计划入口；H 已另行提供有限的 owner jobs 列表/验证恢复和 ProviderCall 人工对账。已有 E 层应用服务可以供后续适配器接入。F 完成判据是本阶段已交付资源、会话、Run、事件、来源、文件和动作的权限隔离与所有认证写请求的 Origin/CSRF 边界，不等于整个产品已可上线。

仅使用 PostgreSQL 5442 的独立测试库验证，业务库 autumn 未迁移或写入；没有部署、发送真实邮件、调用付费模型或推送代码。节点与基础验证结果见 [实施记录](DEVELOPMENT.md)。
