# 秋序前后端接口契约

版本：v1.1。本文约定首版接口，不表示这些端点已经实现。前端实现补充了会话恢复元数据与公开权限版本字段；后续以 FastAPI 输出的 OpenAPI 与本文一致性检查维护契约。

## 1 通用约定

所有业务接口以 /api 开头。JSON 字段采用 snake_case；ID 为 UUID 字符串；时间为带 Z 的 UTC RFC3339 字符串；日窗口与用户显示另附 timezone。金额如出现，使用十进制字符串和独立币种，不能用浮点数相加。

成功返回 {data, request_id}，列表返回 {data: {items, next_cursor}, request_id}。创建通常为 201，受理异步任务为 202，读取或修改为 200，删除成功为 204。列表默认 20 条、最大 100 条，采用服务端生成的游标。

请求携带 Cookie；已认证写请求另带 X-CSRF-Token。GET 不修改业务状态。Idempotency-Key 用于 /ask、站长创建资源或入库、动作预览及执行请求，限制为 128 字符内的随机标识。资源写入去重由 actions 记录，异步入库另用 jobs 的键防止重复排队；统一加用户与操作类型范围。留言使用 client_id 去重。客户端不能提交 role、owner_id、tool_permissions 或任意模型密钥。

资源修改携带 expected_version；与数据库当前版本不符返回 409，不覆盖对方更新。同幂等键但不同请求体返回 409 IDEMPOTENCY_CONFLICT；同键同体返回原结果或原任务。

示例中的 UUID 和文字仅用于说明契约。

### 统一错误

```json
{
  "error": {
    "code": "AI_COOLDOWN",
    "message": "账号仍在 AI 冷却期",
    "details": {
      "cooldown_until": "2026-10-06T02:00:00Z"
    },
    "retry_after_seconds": 3600
  },
  "request_id": "req-example"
}
```

| HTTP | code 示例 | 前端行为 |
| --- | --- | --- |
| 401 | AUTH_REQUIRED、SESSION_EXPIRED | 登录并返回原位置 |
| 403 | EMAIL_UNVERIFIED、STEP_UP_REQUIRED、FORBIDDEN | 展示验证或权限提示 |
| 404 | NOT_FOUND | 不区分不存在和无权读取的私密对象 |
| 409 | VERSION_CONFLICT、IDEMPOTENCY_CONFLICT、CONVERSATION_BUSY | 重新取数或回到已有任务 |
| 409 | ACL_CONTEXT_INVALIDATED | 停止旧上下文输出；重新鉴权并重建上下文后才能进入新的执行阶段 |
| 413 | INPUT_TOO_LARGE、FILE_TOO_LARGE | 提示长度或大小限制 |
| 415 | UNSUPPORTED_FILE_TYPE | 提示仅 PDF 与 DOCX |
| 422 | VALIDATION_ERROR、OCR_REQUIRED、INVALID_URL | 字段级错误或资料提示 |
| 429 | AI_COOLDOWN、QUOTA_EXCEEDED、RATE_LIMITED、CONCURRENCY_LIMIT | 展示截止时间或稍后重试 |
| 502 或 503 | PROVIDER_UNAVAILABLE、SERVICE_UNAVAILABLE | 保留输入，按明确提示重试 |

异步解析失败通过 JobDTO.error 返回 OCR_REQUIRED 或 EXTRACTION_FAILED；HTTP 上传请求本身可已经成功受理。客户端不能把 HTTP 202 当作资料已可检索。

## 2 登录与当前账号

| 方法与路径 | 权限 | 输入与结果 |
| --- | --- | --- |
| POST /api/auth/register | 匿名 | email、password、display_name；统一 202，不暴露邮箱是否已存在 |
| POST /api/auth/verify-email | 一次性 token | token；完成首次验证并设置冷却 |
| POST /api/auth/resend-verification | 匿名 | email；统一 202，限制重发频率 |
| POST /api/auth/login | 匿名 | email、password；设置会话 Cookie，返回本人资料与 csrf_token |
| POST /api/auth/logout | 本人会话 | 撤销当前会话并清 Cookie，204 |
| POST /api/auth/forgot-password | 匿名 | email；统一 202 |
| POST /api/auth/reset-password | 一次性 token | token、new_password；撤销旧会话，要求重新登录 |
| GET /api/auth/me | 可匿名调用 | 未登录返回 user=null；已登录返回状态、能力、csrf_token 和 server_time |
| POST /api/auth/step-up | 已登录站长 | totp_code 或 recovery_code 二选一；返回 step_up_expires_at |
| GET /api/me/quota | 本人会话 | 冷却、当日限额、已用、预占、剩余和下次重置时间 |

/auth/me 和所有认证响应 no-store。csrf_token 只保存在前端内存；密码、Cookie 原始值、TOTP 密钥不出现在响应或日志中。TOTP 初始绑定使用受控引导流程，首版不开放匿名在线站长注册。

### MeDTO

id、display_name、email、role、status、verified_at、ai_cooldown_until、step_up_expires_at、capabilities。capabilities 是当前 UI 提示，不是可回传并获得权限的授权凭据。email 只对本人返回。

### QuotaDTO

timezone、window_start、window_end、daily_limit、used、reserved、remaining、cooldown_until、next_reset_at、server_time。remaining=max(0, daily_limit-used-reserved)，冷却结束仍须满足额度；剩余问答次数不代表联网调用次数。

## 3 公共内容与留言

| 方法与路径 | 权限 | 说明 |
| --- | --- | --- |
| GET /api/public/site | 所有人 | 站名、导航、已公开账号链接和角色展示配置 |
| GET /api/public/notes | 所有人 | 公开文章列表，cursor、limit、tag |
| GET /api/public/notes/{slug} | 所有人 | 当前公开文章 |
| GET /api/public/bookmarks | 所有人 | 只含公开字段的收藏列表 |
| GET /api/public/sources/{publication_id} | 所有人 | 仍有效的资料公开投影 |
| GET /api/public/sources/{publication_id}/file | 所有人 | 仅 raw_download_enabled 且发布未撤回时下载 |
| GET /api/public/comments | 所有人 | resource_id 可空；仅已审核且仍可公开访问的留言 |
| POST /api/comments | 已验证用户 | resource_id 可空、parent_id 可空、body、client_id；返回 pending 或 approved |
| PATCH /api/comments/{id} | 作者本人 | body、expected_version；修改已审核内容重新审核 |
| DELETE /api/comments/{id} | 作者本人或已升级站长 | 主动删除，204 |
| POST /api/reports | 已验证用户 | comment_id、reason；受理举报 |

PublicResourceDTO 为 id、kind、slug、publication_id、publication_no、title、body、note、url、tags、published_at、ai_enabled、raw_download_enabled。未选择公开的 body、note、url 不返回，不能把 private_note 包含在响应再让 UI 隐藏。

CommentDTO 为 id、resource_id、parent_id、author_display_name、body、status、created_at、updated_at、version。公开 DTO 不含作者邮箱；本人可在操作结果中查看自己待审核状态。

## 4 会话与问答

| 方法与路径 | 权限 | 说明 |
| --- | --- | --- |
| POST /api/conversations | 已验证用户 | mode、title 可空；owner 模式要求站长额外验证 |
| GET /api/conversations | 本人 | 按 mode 过滤，分页；owner 模式要求额外验证 |
| GET /api/conversations/{id} | 会话本人 | 读取 ConversationDTO，按固定 mode 检查权限，供刷新恢复使用 |
| GET /api/conversations/{id}/messages | 会话本人 | 分页历史，隐藏已失权的派生内容 |
| PATCH /api/conversations/{id} | 会话本人 | title、expected_version；不能更改 mode |
| DELETE /api/conversations/{id} | 会话本人 | 软删除并安排相关清理，不修改计数 |
| POST /api/ask | 已验证用户 | 冷却、额度、模式和工具检查后返回 202 |
| GET /api/runs/{id} | 运行本人 | 权限有效时返回状态、当前消息、待处理动作 |
| GET /api/runs/{id}/events | 运行本人 | SSE，可用 after 或 Last-Event-ID 续接 |
| POST /api/runs/{id}/cancel | 运行本人 | 请求取消，202；不承诺已发生的动作被撤销 |
| POST /api/runs/{id}/resume | 运行本人 | waiting_input 或 waiting_auth 时恢复，不新建问答次数 |
| GET /api/citations/{id} | 引用所属运行本人 | 对当前仍获准的来源返回摘录与定位 |

mode 为 public 或 owner，创建后固定。public 表示使用公开知识，聊天记录本身不公开。每个会话只有一个非终态运行；需要换题可新建会话。

ConversationDTO 为 id、title、mode、version、created_at、active_run_id。active_run_id 为非终态运行 ID，无活动运行时为 null；返回 ID 不绕过 /runs 的再次鉴权。消息列表返回最近一页，页内按创建时间升序，next_cursor 指向更早的一页。

### AskRequest

```json
{
  "conversation_id": "11111111-1111-4111-8111-111111111111",
  "client_message_id": "22222222-2222-4222-8222-222222222222",
  "message": "帮我找出关于 Agent 记忆的公开文章",
  "resource_ids": [],
  "search_mode": "site"
}
```

search_mode 为 auto、site 或 web。普通用户请求 web 返回 403；auto 也不会分配联网工具。resource_ids 是选择范围而非授权，逐项检查，不能访问的 ID 返回 404。

Idempotency-Key 放请求头，不把它当 conversation_id 使用。AskAccepted 返回 run_id、conversation_id、input_message_id、status、events_url 和 quota。服务端在返回 202 前已持久化消息、额度预占和 job。

### RunDTO

id、conversation_id、status、current_message、pending_actions、input_request、created_at、updated_at、error。status 枚举为 queued、running、waiting_input、waiting_approval、waiting_auth、succeeded、failed、cancelling、cancelled。

MessageDTO 为 id、role、body、content_version、status、created_at、citations。状态为 composing、complete、interrupted 或 hidden；引用字段只返回已授权条目。

ResumeRequest 为 input_request_id 和 answer，可在 waiting_input 时提交补充信息；waiting_auth 时允许仅提交 resume=true。服务端校验等待项仍有效与升级会话有效，不允许任意指定要跳转的图节点。审批类恢复通过 actions/execute 触发，不接受客户端随意设置 approved=true。

input_request 对外仅含 id、prompt、options 和 expires_at。等待项的同答案重复提交返回原结果，不同答案或已替换的等待项返回 409；恢复时仍需取得执行并发槽，未取得返回 429，保留待处理状态。

## 5 SSE 契约

GET /api/runs/{id}/events 返回 text/event-stream，使用 Cookie 鉴权。事件 id 是 run 内递增整数；Last-Event-ID 或 after 只表示已经接收到的序号。两者同时存在时要求一致，否则 422。

心跳不携带业务数据，不改变任务状态。默认可 15 秒心跳；每批业务输出都检查登录、模式与资料权限。重连不重新创建任务，不调用新的模型，不再次扣次。

| event | data 核心字段 | 前端处理 |
| --- | --- | --- |
| run.status | run_id、status | 更新任务状态 |
| message.snapshot | message_id、body、content_version、status、citations | 用较新的版本整体替换累计文本 |
| tool.started | display_name、action_id 可空、summary | 展示允许公开给当前用户的操作摘要 |
| tool.finished | action_id 可空、result_summary | 更新工具卡片 |
| knowledge.processing | job_id、phase、progress | 显示后台任务进度 |
| action.proposed | ActionDTO | 显示具体待确认事项 |
| action.succeeded | action_id、result | 报告已提交的修改 |
| source.invalidated | message_ids、reason | 隐藏相关生成文本并刷新 |
| scope.changed | reason、requires_step_up | 暂停敏感展示并完成验证 |
| error | code、message、retryable | 显示失败原因，不自行生成新 ask |
| done | run_id、status | 关闭流并刷新额度 |

事件回放只从存储的对象 ID 和元数据重新构造当前可读结果，不把历史私密 payload 原样重放。重连先补当前 MessageDTO；快照可能晚于事件游标，客户端凭 content_version 去重。done 只表示终态，不用于 waiting_input 或 waiting_approval；等待时可以关闭连接并显示可恢复状态。

```text
id: 12
event: message.snapshot
data: {"message_id":"33333333-3333-4333-8333-333333333333","body":"找到了两篇相关手记。","content_version":3,"status":"composing","citations":[]}
```

## 6 站长内容与知识库

以下均要求 owner 且额外验证有效。

| 方法与路径 | 请求或结果 |
| --- | --- |
| GET /api/resources | kind、cursor、limit；读取本人资源列表 |
| GET /api/resources/{id} | 当前原稿、版本、现行公开状态 |
| POST /api/resources | 创建 article 或 bookmark；title、正文或 URL、tags、private_note |
| PATCH /api/resources/{id} | expected_version 与变更字段；生成新 revision |
| DELETE /api/resources/{id} | expected_version；软删除、撤回公开、返回清理任务 |
| GET /api/resources/{id}/versions | 版本列表 |
| GET /api/resources/{id}/versions/{revision_id}/file | 鉴权后读取确切版本原文件 |
| POST /api/resources/{id}/publication/preview | revision_id、public_fields、ai_enabled、raw_download_enabled、expected_version、expected_acl_version |
| POST /api/resources/{id}/publication/revoke | expected_version、expected_acl_version；立即撤回 |
| POST /api/knowledge/files | multipart file 与 title；创建资源和解析 job，202 |
| POST /api/knowledge/urls | url、mode、title 可空、tags；收藏或抓取入库，202 |
| POST /api/resources/{id}/refresh | expected_version；仅网页资料手动抓取新版本，202 |
| GET /api/jobs/{id} | 状态、阶段、进度和错误 |
| POST /api/jobs/{id}/cancel | 请求取消 |
| POST /api/jobs/{id}/retry | failed 或 waiting_auth 时重试或恢复；重用资源，避免重复入库 |

knowledge/urls.mode 为 bookmark_only、knowledge_only 或 bookmark_and_knowledge。收藏与网页资料分别是资源，关联但不继承权限。仅收藏时也会校验 URL；获取标题失败可由站长手填，不宣称正文已入库。

ResourceDTO 包括 id、kind、slug、version、acl_version、current_revision、publication、processing_jobs。RevisionDTO 包括 id、revision_no、title、body_text、url、private_note、tags 和受控文件元数据，只有站长可读取。

JobDTO 包括 id、kind、status、phase、progress、resource_id、result、error、can_retry。status 为 queued、running、waiting_auth、succeeded、failed、cancelling、cancelled；phase 可为 fetching、parsing、embedding。progress 不确定时为 null，不编造百分比。

### 公开收藏的例子

```json
{
  "revision_id": "44444444-4444-4444-8444-444444444444",
  "public_fields": ["title", "url", "tags"],
  "ai_enabled": true,
  "raw_download_enabled": false,
  "expected_version": 3,
  "expected_acl_version": 2
}
```

preview 返回 ActionDTO 与预览，不立即发布。站长点击确认后执行 action。Agent 的明确授权指令可通过同一服务直接生成和执行无需再次确认的 action；模型新生成或范围不明的内容必须返回预览。

## 7 操作 设置与审核

| 方法与路径 | 权限与职责 |
| --- | --- |
| GET /api/actions/{id} | 操作本人，按动作重新检查站长验证 |
| POST /api/actions/{id}/execute | 操作本人；参数哈希与 expected_version 匹配，执行既定变更 |
| POST /api/actions/{id}/cancel | 操作本人；仅未执行动作可取消 |
| POST /api/actions/{id}/undo | 站长；仅标记 can_undo 的动作，生成补偿动作 |
| GET /api/settings/ai-limits | 站长；当前冷却、日额度、速率和并发 |
| PATCH /api/settings/ai-limits | 站长；expected_version、值、明确生效范围 |
| GET /api/settings/retention | 站长；当前全部默认 forever |
| POST /api/settings/retention/preview | 站长；scope、mode、ttl_days、include_existing；返回影响及 ActionDTO |
| GET /api/memories | 站长；本人确认过的偏好和事实 |
| POST /api/memories | 站长；content、kind、来源可空 |
| PATCH /api/memories/{id} | 站长本人；expected_version 与内容 |
| DELETE /api/memories/{id} | 站长本人；同时处理派生状态 |
| GET /api/moderation/comments | 站长；按审核状态查询 |
| POST /api/moderation/comments/{id}/decision | 站长；decision、reason、expected_version |
| GET /api/moderation/reports | 站长；待处理举报 |
| POST /api/moderation/reports/{id}/resolve | 站长；resolution |
| GET /api/audit-events | 站长；按对象和时间查询脱敏操作记录 |

ActionDTO 包括 id、type、target、expected_version、expected_acl_version、parameters_hash、summary、changes、impact、requires_confirmation、status、expires_at、can_undo、result。expected_acl_version 在公开权限操作中必填，执行时与内容版本一起重新检查，避免旧预览覆盖已改变的权限。status 为 proposed、awaiting_confirmation、ready、running、succeeded、failed、cancelled、expired。

execute 只提交 action_id 与本次展示的 parameters_hash，不允许替换目标或参数。权限、版本和到期时间均重新检查。已成功执行的同一动作返回既有结果；undo 是新的补偿动作，不修改历史成功记录，也不能撤回已经传播出去的公开内容。

暂不提供通过 AI 修改登录凭据、管理员角色或任意服务器设置的接口。需要的管理能力按白名单逐项加入。

## 8 契约验收

- 前后端使用一致的状态枚举、字段名、时间格式和错误结构。
- 公共 DTO 中不存在 private_note、邮箱、模型提示词和存储对象键。
- 任何 UUID 替换为另一用户对象都不能得到信息。
- 同一个 /ask 幂等键无重复计次；相同内容用新键是新请求。
- SSE 重连只回放仍有权限的事件，累计文本不会因重放重复追加。
- expected_version 不符时返回冲突，不静默覆盖。
- 入库、发布和撤回均返回真实状态，允许后台索引尚未完成。
