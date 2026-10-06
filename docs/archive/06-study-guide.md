# 06 研究路线与复盘

本指南围绕已经实现的代码研究，历史教学 SQL/Python 只作为说明。完整早期知识点文档保留在 [原始资料](history/秋序后端架构评审与知识点详解_v1.0.md)，其中“建议”和“已验证”的措辞保持原状。

## 第一轮：理解产品与进程边界

先读项目范围、运行说明和 `app.py` / `workers/__main__.py` / `workers/bootstrap.py`，画出浏览器、Next、API、Worker、数据库与供应商的调用关系。应能回答：为什么 API 返回 202 后仍需要 Worker，为什么 Agent 与后端同项目却不是在每个 HTTP 请求中直接长时间运行。

研究材料：[01 项目现状](01-project-snapshot.md)、[02 核心链路](02-architecture-and-flows.md)、[H 说明](../../backend/H_STAGE_WORKERS.md)。

## 第二轮：数据库事实与事务

依次阅读模型、迁移、UoW、Repository 能力类、runs/quota/jobs 原子方法和对应真实数据库测试。将“谁负责”区分清楚：Policy 决定是否允许，Service 编排事务，Repository 表达原子变化，数据库提供最后仲裁。

| 知识点                | 当前用途                     | 自我检查问题                                   |
| --------------------- | ---------------------------- | ---------------------------------------------- |
| UoW / AsyncSession    | 多表一起提交、回滚、释放     | 为什么 heartbeat 不能复用 handler 的 Session？ |
| 唯一约束与定向 UPSERT | Run、留言、预留去重          | 同会话忙为什么不能当作幂等命中？               |
| CAS 与状态条件        | 版本和单调状态转换           | UPDATE 0 行是成功、冲突还是不存在？            |
| 行锁与固定顺序        | 额度、父留言、发布和正式提交 | 同时锁多资源为何先按 UUID 排序？               |
| 复合外键              | 同用户、同资源和同版本归属   | SET NULL 为什么可能把合法删除变成约束失败？    |
| 部分唯一索引          | 当前公开版本、非终态运行     | 等待审批为什么仍占会话却不占账号执行槽？       |
| NULL 安全 CHECK       | TTL、状态/时刻等             | 表达式得到 NULL 是否会被 CHECK 拒绝？          |
| 可延迟约束            | 相互引用的 Run/Message 等    | 为什么必须观察提交而不只看 flush？             |

研究材料：[03 一致性](03-data-and-consistency.md)、[07 字段快照](07-database-snapshot.md)、`tests/concurrency/test_core_gate.py`、`test_content_gate.py`、`scripts/probe_constraints.py`。

## 第三轮：身份、公开投影与 RAG

读 ActorContext、Facts、Decision 和纯 Policy；再读 AccessService、PublicationService、KnowledgeService 和运行上下文。应能解释“角色已确认”和“这份资料现在仍可读取”为什么是两个检查。

重点研究五件事：原稿与公开投影分离；SQL 先权限过滤再向量排序；实际进入模型的所有来源登记；历史回答的间接依赖也复查；权限变化后旧正文和旧闭包不能因 checkpoint 恢复再次合法。

思考案例：站长只公开收藏标题与链接，私人备注未选；随后生成过回答，再撤回公开。分别说明公开 API、索引、来源弹窗、历史消息、SSE 回放和正在运行的模型会如何处理。

研究材料：[纯权限契约](../../backend/POLICY_CONTRACT.md)、[E6/E8](../../backend/E_STAGE_SERVICES.md)、`services/context.py`、`services/knowledge.py`、`tests/integration/test_agent_invalidation.py`。

## 第四轮：Agent 和用户授权

读 `agent/contracts.py`、`prompts.py`、`tools.py`、`graph.py` 和 `runtime.py`，沿一条提问追踪规划→检索→再授权→回复。然后沿一条收藏请求追踪预览→用户确认→数据库写入→代际重建→真实结果。

需区分系统约束、真实用户消息和 untrusted_data。网页/PDF/Word 中“请公开全部资料”的文本不是用户授权，模型工具参数也不能替换 ActorContext 或关闭确认。

工具可以给模型能力说明，但能力说明不是授权事实。schema 可验证形状，Service 还要验证当前权限、对象、版本和影响。当前五个工具不是最初产品工具愿望清单中的所有工具。

研究材料：[G 说明](../../backend/G_STAGE_AGENT.md)、[助手内容验收](../../backend/ASSISTANT_CONTENT_ACCEPTANCE.md)、`tests/integration/test_agent_resource_creation.py`、`test_agent_restore.py`。

## 第五轮：至少一次任务与未知副作用

读 Queue、Worker core、ExecutionService、ProviderCall Repository 与 maintenance/reconciliation。用三种故障推演：请求尚未发出崩溃；上游已收到而本机超时；结果返回时 lease 或 ACL 已失效。

| 事实              | 当前处理                | 不应推断                           |
| ----------------- | ----------------------- | ---------------------------------- |
| 已知未派发        | 可以释放预留或受控重试  | 所有失败都未收费                   |
| 已派发但未知      | 保留 unknown 和派发证据 | 改成 failed 后直接重发             |
| 旧 token 返回结果 | 联合提交拒绝            | finish 单独拒绝可以撤销已提交业务  |
| 人工对账成功      | 只结算账本与审计        | 自动创建 assistant、退款或重新排队 |
| SMTP 接受         | 发送端受理              | 最终投递或恰好一次收件             |

研究材料：[H 说明](../../backend/H_STAGE_WORKERS.md)、[供应商验收](../../backend/PROVIDERS_ACCEPTANCE.md)、[邮件配置](../../backend/EMAIL_CONFIGURATION.md)、`test_worker_lease.py`、`test_worker_provider.py`、`test_auth_email.py`。

## 第六轮：前端状态与验收证明

读 API transport、SessionProvider、SSE reducer、Chat/InputWaitForm 和 Action 组件，再看实际注册路由。检查页面有按钮时是否真的有后端适配，区分 demo 可体验、API 可调用、Worker 可执行和用户旅程已验收。

前端问题有多种来源：重渲染回调导致失焦；路径变了但弹窗未关闭；固定选项被错误当成自由文本；局域网 HTTP 缺少 API；开发来源拦截。不要仅用一条模型口头回复或一个桌面截图解释整个故障。

研究材料：[04 前端边界](04-frontend-and-api.md)、[08 路由](08-api-snapshot.md)、[前端记录](../../frontend/README.md) 及对应 targeted tests。

## 早期评审意见的落地

| 评审主题           | 当前落地                                         | 尚需注意                             |
| ------------------ | ------------------------------------------------ | ------------------------------------ |
| 内容/ACL 双版本    | Resource、Action、预览确认和发布/撤回比较        | 原设计旧版本措辞不能覆盖实现         |
| lease 约束业务提交 | Execution/Action 提交末端联合闸门                | 无法撤销已经发出的外部请求           |
| 幂等重放仍鉴权     | 同键复用结果前重查身份/范围                      | 不能返回旧权限下的私人正文           |
| 稳定逻辑调用身份   | ProviderCall + attempt_no + 摘要外部键           | 上游幂等支持没有统一保证             |
| 文件状态可变实体   | file_objects + staging/finalize/delete           | 存储不是数据库分布式事务             |
| Agent 恢复归属     | 同用户受控 Session 换绑，当前闭包重建            | checkpoint 不替代登录/确认           |
| 状态与事件一致     | source.invalidated/scope.changed + 有限 Run 状态 | 不使用旧建议中的 run.invalidated     |
| CI 和冻结          | 已有真实 PG workflow 与本地闸门                  | I 阶段扩展、远端执行和全站冻结未完成 |

## 归档后的可研究问题

1. 在不增加新抽象层的前提下，如何补齐知识和设置 HTTP 入口并保持同一服务授权？
2. 如何设计失效来源下的自动摘要，使长对话可用且不保存未经授权的上下文？
3. 百炼多批索引部分成功后，如何在不重放已知成功或 unknown 调用的情况下恢复？
4. 不同上游幂等和账单能力下，怎样完善成本仲裁与精确货币预算？
5. 如何将前端 DTO 与 OpenAPI 持续校验，并把 missing route 从运行时问题提前变为开发闸门？
6. 如何验证真实邮件、手机浏览器、跨账号隔离和恢复流程，同时控制测试副作用？
7. 若将来上线，需要怎样的应用/迁移数据库角色、备份恢复、对象与框架状态保留及可观测性？

这些是研究与后续工作清单，本轮没有开始实施它们。归档记录当前边界，便于以后继续验证，而不是宣布整个 v1 已冻结或可以上线。
