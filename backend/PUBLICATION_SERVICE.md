# 发布应用服务（E1）

实现位置：services/publication.py。服务接收服务端 ActorContext 和不可变命令，
返回不可变 PublicationResult；ORM 记录不会离开事务。当前没有对应 HTTP 端点。

## 入口与授权

publish_explicit / revoke_explicit 只用于用户明确指定已有资源、版本和公开字段的
请求。调用方必须是已完成当前额外验证的站长，身份从服务端入口注入。方法内部
生成 explicit_request Action，不接收客户端指定的授权类型、角色或确认开关。
模型新生成或范围不明的内容由 E7 的持久预览/确认流程承接，不能直接走此入口。
Agent 消息授权与预览确认尚未实现；E7/G 仍须保存实际授权消息及操作证据。

事务先按 user → auth_session → resource 顺序加锁。会话查询先过滤用户归属，
再装配当前账号、会话版本、撤销、升级和到期事实。采用数据库实际时钟，权限在
写入前和提交前检查。缺失及跨账号资源统一 NOT_FOUND；本人需要升级则为
STEP_UP_REQUIRED。未来禁用/撤销认证流程也须遵守 user → auth_session 锁顺序。

## 发布与撤回事务

1. 鉴权、锁资源，校验 expected_version 和 expected_acl_version。
2. 校验 revision_id 属于资源；按资源类型白名单选择实际有值的字段。
3. 撤销旧投影并插入新投影，或撤销当前投影。
4. 增加资源 acl_version 与全站 content_acl_epoch。内容 version 保持不变。
5. 同事务写 Action 成功结果、脱敏 AuditEvent 与公开索引同步 Job；最后重新鉴权。

原稿字段 body_text 映射为公开 body，private_note 映射为公开 note。也接受规范化
后的 body/note；两种写法生成相同参数摘要。五个公开字段为 title/body/note/url/tags，
各类型明确使用这份白名单且只允许选择实际有值的字段；不自动复制未选择的内容。
首版仅 document 可以启用原文件下载，且所选原稿必须关联原文件。

撤回只对实际存在的当前投影递增 ACL/epoch 并创建同步作业。没有投影时为成功的
无变化操作，仍记录这次明确请求与审计。公开读取继续使用 Repository 的实时联表
过滤，不等待异步索引清理。全部步骤都只有数据库 I/O，没有供应商或存储调用。

## 幂等与结果

Action 使用 (actor_id, idempotency_key) 唯一身份。参数摘要包含资源、原稿、双版本、
规范化公开字段与开关；类型和授权方式也须一致。同键不同参数返回
IdempotencyConflictError（接口层将映射 IDEMPOTENCY_CONFLICT）。同键成功操作
先按当前权限检查，再返回原结果，不重新发布、增加 ACL、入队或写审计。
明确请求的 ready → succeeded 同时校验授权方式、确认开关、状态和实际到期时间。

结果的 resource_version / acl_version / scope_epoch 是该动作成功时记录的版本，
不能当作当前资源的编辑凭据；后续修改前重新读取 ResourceDTO。
is_current / public_url 反映当前公开状态：旧发布结果在撤回后重放时不再给出有效
公开 URL。actual_public_fields 是该动作涉及投影实际选中的字段名，正文不进 Action。
安全文章 slug 返回 /notes/{slug}，其它投影返回 /api/public/sources/{publication_id}。
这些是后续 F 接口的路径契约，当前服务完成并不表示 HTTP 路由已经上线。
index_job_id / index_job_status 返回真实队列记录，不把入队描述为 AI 索引已就绪。

## 公开索引作业契约

Job.kind 为 knowledge.publication_sync，幂等键包含 Action ID。
payload 只含 scope=public、resource_id、publication_id、acl_version、scope_epoch
和 ai_enabled。撤回时 publication_id 为 null、ai_enabled 为 false。
载荷不含私人正文、原稿片段或私人索引/向量标识。

E6/H 的 handler 必须重新检查当前资源、ACL 和现行 publication。旧作业不能
覆盖新发布索引或清理较新权限版本的索引。构建输入只能来自明确选中的公开投影，
不得读取私人 chunk 后改 public 标记；撤回或关闭 AI 时清理/退役相关公开索引。
正式结果写入还须按 H 校验有效租约。当前节点只保证同步作业与发布一起持久化，
实际构建、退役和 worker 尚待后续实现。

## 本轮基础验证

PostgreSQL 5442 的独立测试库运行三个用例：发布/重试/撤回，权限/版本拦截，
晚期故障导致完整回滚。回滚用例只在审计位置注入故障，之前的修改由真实数据库
完成；不将其作为并发正确性证明。按用户要求，本轮保持基础测试范围。
Ruff、格式、mypy strict 和提交差异检查通过；没有新增依赖、表或迁移。
