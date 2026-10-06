# 08 已注册业务路由快照

归档日期：2026-10-06。通过 Python AST 读取业务路由装饰器，并核对 app.py 的 include_router。
不包含 FastAPI 自动提供的文档入口；HTTP 状态是装饰器默认/声明值，运行时分支仍可能返回其他状态。
路由存在不代表完整浏览器联调已通过；鉴权、请求字段与错误应继续阅读对应契约和源码。

共 **53 个业务路由**。

| 方法   | 路径                                                       | 声明状态 | 处理器            | 源文件                                                                           |
| ------ | ---------------------------------------------------------- | -------- | ----------------- | -------------------------------------------------------------------------------- |
| GET    | `/api/actions/{action_id}`                                 | 200      | `read`            | [代码](../../backend/src/autumn_backend/api/actions.py) 第 34 行                 |
| POST   | `/api/actions/{action_id}/cancel`                          | 200      | `cancel`          | [代码](../../backend/src/autumn_backend/api/actions.py) 第 53 行                 |
| POST   | `/api/actions/{action_id}/execute`                         | 202      | `execute`         | [代码](../../backend/src/autumn_backend/api/actions.py) 第 39 行                 |
| POST   | `/api/ask`                                                 | 202      | `ask`             | [代码](../../backend/src/autumn_backend/api/chats.py) 第 128 行                  |
| POST   | `/api/auth/forgot-password`                                | 202      | `forgot`          | [代码](../../backend/src/autumn_backend/api/auth.py) 第 158 行                   |
| POST   | `/api/auth/login`                                          | 200      | `login`           | [代码](../../backend/src/autumn_backend/api/auth.py) 第 138 行                   |
| POST   | `/api/auth/logout`                                         | 204      | `logout`          | [代码](../../backend/src/autumn_backend/api/auth.py) 第 150 行                   |
| GET    | `/api/auth/me`                                             | 200      | `me`              | [代码](../../backend/src/autumn_backend/api/auth.py) 第 102 行                   |
| POST   | `/api/auth/register`                                       | 202      | `register`        | [代码](../../backend/src/autumn_backend/api/auth.py) 第 111 行                   |
| POST   | `/api/auth/resend-verification`                            | 202      | `resend`          | [代码](../../backend/src/autumn_backend/api/auth.py) 第 130 行                   |
| POST   | `/api/auth/reset-password`                                 | 200      | `reset`           | [代码](../../backend/src/autumn_backend/api/auth.py) 第 164 行                   |
| POST   | `/api/auth/step-up`                                        | 200      | `step_up`         | [代码](../../backend/src/autumn_backend/api/auth.py) 第 176 行                   |
| POST   | `/api/auth/verify-email`                                   | 200      | `verify_email`    | [代码](../../backend/src/autumn_backend/api/auth.py) 第 122 行                   |
| GET    | `/api/citations/{citation_id}`                             | 200      | `citation`        | [代码](../../backend/src/autumn_backend/api/chats.py) 第 172 行                  |
| POST   | `/api/comments`                                            | 201      | `create`          | [代码](../../backend/src/autumn_backend/api/comments.py) 第 75 行                |
| DELETE | `/api/comments/{comment_id}`                               | 204      | `delete`          | [代码](../../backend/src/autumn_backend/api/comments.py) 第 96 行                |
| PATCH  | `/api/comments/{comment_id}`                               | 200      | `edit`            | [代码](../../backend/src/autumn_backend/api/comments.py) 第 88 行                |
| GET    | `/api/conversations`                                       | 200      | `conversations`   | [代码](../../backend/src/autumn_backend/api/chats.py) 第 73 行                   |
| POST   | `/api/conversations`                                       | 201      | `create`          | [代码](../../backend/src/autumn_backend/api/chats.py) 第 62 行                   |
| DELETE | `/api/conversations/{conversation_id}`                     | 204      | `delete`          | [代码](../../backend/src/autumn_backend/api/chats.py) 第 120 行                  |
| GET    | `/api/conversations/{conversation_id}`                     | 200      | `conversation`    | [代码](../../backend/src/autumn_backend/api/chats.py) 第 88 行                   |
| PATCH  | `/api/conversations/{conversation_id}`                     | 200      | `rename`          | [代码](../../backend/src/autumn_backend/api/chats.py) 第 107 行                  |
| GET    | `/api/conversations/{conversation_id}/messages`            | 200      | `messages`        | [代码](../../backend/src/autumn_backend/api/chats.py) 第 95 行                   |
| GET    | `/api/me/quota`                                            | 200      | `quota`           | [代码](../../backend/src/autumn_backend/api/quota.py) 第 13 行                   |
| GET    | `/api/moderation/comments`                                 | 200      | `moderation`      | [代码](../../backend/src/autumn_backend/api/comments.py) 第 115 行               |
| POST   | `/api/moderation/comments/{comment_id}/decision`           | 200      | `decide`          | [代码](../../backend/src/autumn_backend/api/comments.py) 第 130 行               |
| GET    | `/api/moderation/reports`                                  | 200      | `reports`         | [代码](../../backend/src/autumn_backend/api/comments.py) 第 140 行               |
| POST   | `/api/moderation/reports/{report_id}/resolve`              | 200      | `resolve`         | [代码](../../backend/src/autumn_backend/api/comments.py) 第 155 行               |
| GET    | `/api/owner/jobs`                                          | 200      | `waiting`         | [代码](../../backend/src/autumn_backend/api/tasks.py) 第 23 行                   |
| POST   | `/api/owner/jobs/{job_id}/resume`                          | 202      | `resume`          | [代码](../../backend/src/autumn_backend/api/tasks.py) 第 31 行                   |
| GET    | `/api/owner/provider-calls/unknown`                        | 200      | `unknown`         | [代码](../../backend/src/autumn_backend/api/provider_reconciliation.py) 第 24 行 |
| POST   | `/api/owner/provider-calls/{call_id}/reconcile`            | 200      | `confirm`         | [代码](../../backend/src/autumn_backend/api/provider_reconciliation.py) 第 31 行 |
| GET    | `/api/public/bookmarks`                                    | 200      | `bookmarks`       | [代码](../../backend/src/autumn_backend/api/public.py) 第 78 行                  |
| GET    | `/api/public/comments`                                     | 200      | `public_comments` | [代码](../../backend/src/autumn_backend/api/comments.py) 第 62 行                |
| GET    | `/api/public/notes`                                        | 200      | `notes`           | [代码](../../backend/src/autumn_backend/api/public.py) 第 68 行                  |
| GET    | `/api/public/notes/{slug}`                                 | 200      | `note`            | [代码](../../backend/src/autumn_backend/api/public.py) 第 88 行                  |
| GET    | `/api/public/site`                                         | 200      | `site`            | [代码](../../backend/src/autumn_backend/api/public.py) 第 54 行                  |
| GET    | `/api/public/sources/{publication_id}`                     | 200      | `source`          | [代码](../../backend/src/autumn_backend/api/public.py) 第 93 行                  |
| GET    | `/api/public/sources/{publication_id}/file`                | 200      | `file`            | [代码](../../backend/src/autumn_backend/api/public.py) 第 98 行                  |
| POST   | `/api/reports`                                             | 201      | `report`          | [代码](../../backend/src/autumn_backend/api/comments.py) 第 104 行               |
| GET    | `/api/resources`                                           | 200      | `list_resources`  | [代码](../../backend/src/autumn_backend/api/resources.py) 第 33 行               |
| POST   | `/api/resources`                                           | 201      | `create`          | [代码](../../backend/src/autumn_backend/api/resources.py) 第 81 行               |
| DELETE | `/api/resources/{resource_id}`                             | 204      | `delete`          | [代码](../../backend/src/autumn_backend/api/resources.py) 第 102 行              |
| GET    | `/api/resources/{resource_id}`                             | 200      | `read`            | [代码](../../backend/src/autumn_backend/api/resources.py) 第 48 行               |
| PATCH  | `/api/resources/{resource_id}`                             | 200      | `edit`            | [代码](../../backend/src/autumn_backend/api/resources.py) 第 90 行               |
| POST   | `/api/resources/{resource_id}/publication/preview`         | 201      | `preview`         | [代码](../../backend/src/autumn_backend/api/public.py) 第 112 行                 |
| POST   | `/api/resources/{resource_id}/publication/revoke`          | 200      | `revoke`          | [代码](../../backend/src/autumn_backend/api/public.py) 第 133 行                 |
| GET    | `/api/resources/{resource_id}/versions`                    | 200      | `versions`        | [代码](../../backend/src/autumn_backend/api/resources.py) 第 53 行               |
| GET    | `/api/resources/{resource_id}/versions/{revision_id}/file` | 200      | `file`            | [代码](../../backend/src/autumn_backend/api/resources.py) 第 65 行               |
| GET    | `/api/runs/{run_id}`                                       | 200      | `run`             | [代码](../../backend/src/autumn_backend/api/chats.py) 第 145 行                  |
| POST   | `/api/runs/{run_id}/cancel`                                | 202      | `cancel`          | [代码](../../backend/src/autumn_backend/api/chats.py) 第 150 行                  |
| GET    | `/api/runs/{run_id}/events`                                | 200      | `events`          | [代码](../../backend/src/autumn_backend/api/events.py) 第 35 行                  |
| POST   | `/api/runs/{run_id}/resume`                                | 202      | `resume`          | [代码](../../backend/src/autumn_backend/api/chats.py) 第 157 行                  |

## 当前未注册的主要计划入口

- 前端知识库使用的 `/api/knowledge/files`、`/api/knowledge/urls`。
- 前端任务卡片使用的通用 `/api/jobs/{id}`、取消与重试。现有 `/api/owner/jobs` 是另一套有边界的列表/验证恢复接口。
- 前端设置页使用的 `/api/settings/ai-limits`、`/api/settings/retention`、保留策略预览。
- 前端使用的 `/api/memories` 与 `/api/audit-events` 管理入口。

相关应用服务、动作 handler 或数据表部分已存在，仍需补齐 HTTP 适配及联调；不能用演示成功说明真实模式可用。
