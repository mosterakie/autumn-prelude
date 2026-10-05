# v1 数据契约覆盖与映射

本文回答实施顺序 A7 与《实施顺序文档评审与 A 阶段验收》D1 提出的问题：
**v1 的每一项支撑功能，数据落在哪张表、由谁维护、在哪个阶段交付。**

排序总原则：先让数据库能证明不变量，再让数据访问层能原子改状态。
本表是"哪一项存在哪里"的**权威索引**；字段与约束的细节以
[数据库设计](../../docs/architecture/database.md) 与模型代码为准。

- 状态：**已建** = 28 张表内的实体；**已有 JSON 结构** = 落在某个 JSONB 列里；
  **延期** = 明确不做或不在此阶段做，并写明理由与承接位置。
- 共 **28 张应用表**，分四个迁移批次，与 `src/autumn_backend/db/models/` 的四个模块一一对应。

---

## 1 表清单（按迁移批次）

| 批次 | 迁移 | 表 |
|---|---|---|
| 一（A3/A4） | `identity foundation` | users / auth_sessions / auth_tokens / admin_factors / rate_limit_buckets / settings |
| 二（A5） | `content files comments reports` | retention_policies / resources / file_objects / resource_versions / publications / comments / reports |
| 三（A6） | `run quota job audit` | conversations / runs / messages / actions / quota_buckets / quota_reservations / jobs / provider_calls / audit_events / run_events |
| 四（A7） | `knowledge sources summaries memories` | knowledge_indexes / knowledge_chunks / run_sources / conversation_summaries / memories |

---

## 2 v1 支撑功能的存储归属

| 功能 | 存储实体 | 状态 | 交付阶段 | 说明 |
|---|---|---|---|---|
| 账号与会话 | `users` / `auth_sessions` | 已建 | A3 | 邮箱规范化唯一；`auth_version` 是身份失效闸门 |
| 一次性令牌 | `auth_tokens` | 已建 | A3 | 只存哈希；用途限 verify_email / reset_password |
| 站长 TOTP | `admin_factors` | 已建 | A3 | 密钥只存密文；`last_used_time_step` 防重放；恢复码只存哈希 |
| 登录/邮件限流 | `rate_limit_buckets` | 已建 | A3 | `scope_hash` 是服务端 HMAC，不存明文邮箱 |
| 全站配置 | `settings` | 已建 | A3 | `content_acl_epoch` 由迁移补种，缺行不得当成有效的 0 |
| 保留策略 | `retention_policies` | 已建 | A5 | 默认全部 forever；给历史数据设 `expires_at` 是可预览的独立作业 |
| 内容主体 | `resources` | 已建 | A5 | `version` 与 `acl_version` 解耦；`current_revision_id` 复合外键绑定本资源版本 |
| 内容版本 | `resource_versions` | 已建 | A5 | 标题/正文/URL/私密备注/标签都在**版本**上；创建后不可编辑 |
| 公开投影 | `publications` | 已建 | A5 | 类型化 `public_*` 列 + `public_fields` 白名单，越界组合被 CHECK 拒绝 |
| 文件生命周期 | `file_objects` | 已建 | A5 | `staged`/`ready`/`pending_delete`/`failed`；物理 I/O 不在事务内，靠 job 收敛（E5/H4） |
| 留言与审核 | `comments` | 已建 | A5 | 待审核流程；`resource_id` 可空用于独立留言板；父子同资源由复合外键保证 |
| 举报 | `reports` | 已建 | A5 | 每用户每留言最多一条 `open`（部分唯一索引）；F8 出接口 |
| 对话 | `conversations` | 已建 | A6 | `mode` 创建后不可改；`next_message_seq` 分配消息序号 |
| 消息正文 | `messages` | 已建 | A6 | 完整正文；`content_version` 是**累计正文版本**，供 SSE 快照替换 |
| 运行 | `runs` | 已建 | A6 | 幂等键唯一；同会话非终态唯一；`checkpoint_thread_id` 服务端生成 |
| 流式事件 | `run_events` | 已建 | A6 | 只追加；序号由 `runs.next_event_seq` 原子分配 |
| 等待补充信息 | `runs.input_request`（JSONB） | 已有 JSON 结构 | A6/E2 | 由版本化 schema 校验；等待项含 expires_at / answer_hash |
| 需确认的动作 | `actions` | 已建 | A6 | 幂等键不可空；**双版本** `expected_version` + `expected_acl_version` |
| 授权来源 | `actions.authorization_kind`（枚举） | 已建 | A6 | explicit_request / confirmed_preview |
| 每日额度 | `quota_buckets` / `quota_reservations` | 已建 | A6 | 桶**不存静态限额**，限额从当前配置读取 |
| 外部调用与成本 | `provider_calls` | 已建 | A6 | 稳定 `logical_call_key` + 物理 `attempt_no`；`unknown` 不是零费用 |
| 持久作业 | `jobs` | 已建 | A6 | lease + SKIP LOCKED；`waiting_auth` 支持验证过期后恢复 |
| 操作审计 | `audit_events` | 已建 | A6 | 只追加；`before/after_version` 专指 resources.version |
| 知识索引元数据 | `knowledge_indexes` | 已建 | A7 | scope=public 必须绑 publication；活动索引必须 ready |
| 知识分块与向量 | `knowledge_chunks` | 已建 | A7 | `vector(1024)`；首版精确查询，**不建近似索引** |
| 模型来源依赖 | `run_sources` | 已建 | A7 | 记录全部实际进入模型的来源，含间接依赖；`UNIQUE(run_id, source_key)` |
| 会话摘要 | `conversation_summaries` | 已建 | A7 | 同会话最多一个 active；恢复时必须重查依赖 |
| 记忆 | `memories` | 已建 | A7 | 首版仅站长明确要求时创建；来源 run 用单列 FK + SET NULL（不阻止删除会话），同用户归属由 service 校验 |
| 联网来源展示 | `run_sources.web_url` / `web_title` / `fetched_at` / `excerpt` | 已有列与 JSON 结构 | A7/E6 | 抓取的网页正文统一落为 `resource_versions` |
| 消息展示状态 | `messages.status`（枚举） | 已建 | A6 | composing / complete / interrupted / hidden |
| 内容结构版本 | `settings.schema_version` / `resource_versions.content_format` | 已有列 | A3/A5 | 与 `messages.content_version`（累计正文版本）**含义不同**，不可混用 |
| LangGraph 框架状态 | 独立 schema（如 `agent_state`） | **延期** | G1 | 表结构由固定版本的持久化适配器维护，**不由本项目建模**；应用只持有 conversation → `runs.checkpoint_thread_id` 映射 |
| 应用运行日志 | 外部日志存储 | **延期** | I2 | 默认永久，字段含 request_id / run_id / error_code / 耗时；当前只保留接口，不为原始日志新建通用业务表 |
| 账号彻底销号的匿名化 | — | **延期** | 未定 | 文档明确"不在首版范围" |

---

## 3 DB 枚举 ↔ API DTO 映射

要求：**同名同值**，不做转换层。唯一不在 DTO 中暴露的是纯内部词表。

| DB 枚举 | API DTO / 字段 | 关系 |
|---|---|---|
| `RunStatus` | `RunDTO.status` | 同名同值（queued / running / waiting_input / waiting_approval / waiting_auth / succeeded / failed / cancelling / cancelled） |
| `JobStatus` | `JobDTO.status` | 同名同值（queued / running / waiting_auth / succeeded / failed / cancelling / cancelled） |
| `JobPhase` | `JobDTO.phase` | DTO 列出的 fetching / parsing / embedding 全部包含；额外有 indexing / finalizing / deleting 三个存储与索引 job 自身的阶段 |
| `ActionStatus` | `ActionDTO.status` | 同名同值（proposed / awaiting_confirmation / ready / running / succeeded / failed / cancelled / expired） |
| `ActionType` | `ActionDTO.type` | 服务端白名单初始集合；新增类型须改迁移 |
| `MessageStatus` | SSE `message.snapshot.status` | 同名同值（composing / complete / interrupted / hidden） |
| `MessageRole` | `MessageDTO.role` | user / assistant；工具协议消息保存在运行状态中，不混入对话展示 |
| `CommentStatus` | `CommentDTO.status` | 同名同值（pending / approved / rejected / hidden） |
| `ResourceKind` | `ResourceDTO.kind` | 同名同值（article / bookmark / document / webpage） |
| `ContentFormat` | `RevisionDTO` 内部 | markdown / plain |
| `ReportStatus` | 审核接口 | open / resolved / dismissed |
| `RetentionScope` / `RetentionMode` / `RetentionAnchor` | `/api/settings/retention` 入参 | 同名同值 |
| `RunEventType` | SSE 事件名 | 同名同值（11 个事件） |
| `UserStatus` / `UserRole` | 内部 | 不对外暴露状态；`role` 决定能力 |
| `IndexScope` / `IndexStatus` | 内部 | 检索侧过滤条件，不作为 DTO 字段 |
| `FileObjectStatus` | 内部 | 由 `JobDTO` 的进度/结果体现，不直接暴露 |
| `AuditResult` | 内部 | `event_type` 用 `domain.verb` 形状约束而不冻结取值 |

---

## 4 有意偏离文档之处

评审要求"文档约束存在、`alembic check` 成功与业务语义正确是不同层次"，
因此每处偏离都显式记录，而不是让读者自己猜。

| 位置 | 偏离 | 理由 |
|---|---|---|
| 所有 UUID 主键 | 除 `default=uuid4` 外另加 `server_default=gen_random_uuid()` | 文档要求"由应用生成"；保留数据库兜底，让原生 SQL 与 Repository 的 UPSERT 不必自己生成主键 |
| `provider_calls` | 增加 `logical_call_key` 并以 `UNIQUE(logical_call_key, attempt_no)` 取代 `UNIQUE(job_id, purpose, attempt_no)` | 文档只给 `attempt_no`；评审 D10 指出物理 attempt 不能代替逻辑调用身份，且 `job_id` 可空时唯一约束会被 NULL 语义破坏 |
| `actions` | 保留 `expected_acl_version` | `database.md` 未列该列，但接口契约的 `ActionDTO` 要求它，且评审 D3 明确要求双版本 |
| `run_events.type` | 用枚举而不是 `text` | 接口契约已冻结 11 个 SSE 事件名；用同一份词表约束可避免前端契约静默漂移 |
| `audit_events.event_type` | `text` + `domain.verb` 形状 CHECK | 审计类型会演进，冻结取值会迫使频繁改迁移；形状约束仍能拦住"随手写个字符串" |
| `file_objects` | `database.md` 批次清单之外的新表 | 评审 §6 要求文件 `staged`/`ready`/`pending_delete` 必须落在**可变**记录上，而不是反复修改声明不可变的 `resource_versions` |
| `quota_reservations` | 用 `(bucket_id, user_id)` 复合外键取代文档建议的"约束触发器校验" | 复合外键同样能表达"run 用户与桶用户一致"，且更早失败、被 `alembic` 与 catalog 核对看得见 |
| `conversations.thread_id` | 移到 `runs.checkpoint_thread_id` | 文档把框架线程标识放在 `runs` 上；应用只持有 conversation → thread 的映射 |
| `knowledge_chunks` | 不建 HNSW/IVFFlat 近似索引 | 文档 §5 明确"首版精确向量查询，不建近似索引" |
| `retention_policies` | 用两个部分唯一索引取代 `NULLS NOT DISTINCT` | 文档允许二选一；显式两个索引更直观，且不依赖方言开关与 Alembic 支持度 |
| `provider_calls.estimated_cost` / `actual_cost` | `numeric(20,8)`；库中存储用 `Numeric` | 文档要求；有费用则必须有 `currency`，不同币种不能直接相加 |
| `resources.current_revision_id`、`messages.run_id`、`runs.auth_session_id` | 删除动作由 `SET NULL` 改为 `NO ACTION`（仍可延迟） | **修的是真缺陷**：`ON DELETE SET NULL` 会把外键列组里**每一列**都置空，而这三处的列组都含 NOT NULL 列（`id` 主键 / `conversation_id` / `user_id`），因此该动作永远以 `NotNullViolation` 失败，"解开引用"根本不可能发生。改为 `NO ACTION` 后由 service 先摘引用；删除整个会话/资源时子行一起级联，提交时无悬挂引用，约束不会拦 |
| `memories.origin_run_id` | 由 `(origin_run_id, user_id)` 复合外键改为**单列**外键 + `SET NULL` | 同上一类缺陷，但语义不同：记忆是来源的**旁证**，不该因为"来源 run 还在"而阻止删除会话，所以保留 SET NULL（单列时它能正常工作）。"来源 run 属于同一用户"改由 service 校验，测试用例显式钉住了"库不再做同用户校验"这件事 |
| `retention_policies` 的 TTL CHECK | 写成 NULL 安全形式：`(mode='forever' AND ttl_days IS NULL) OR (mode='ttl' AND ttl_days IS NOT NULL AND ttl_days > 0)` | **修的是真缺陷**：原式在 `mode='ttl'` 且 `ttl_days IS NULL` 时求值为 NULL，而 PostgreSQL 的 CHECK 只拒绝 FALSE，于是"ttl 模式却没有天数"会漏过去，与文档 §9 不符 |
| `knowledge_chunks` | 不加与 `UNIQUE(index_id, chunk_no)` 同列的普通 B-tree 索引 | 唯一约束的复合索引以 `index_id` 为前导列，前缀查询直接可用；再建一个是纯冗余（写放大、占空间）。文档 §5 要求的 `B-tree(index_id)` 由该唯一约束满足 |

### 默认值规则（写入方必须知道）

**`NOT NULL` 且只有 Python 侧默认值的列，必须由写入方显式提供。**
这是有意的：默认值属于应用语义（例如 `users.role` 默认 `member`、
`status` 默认 `pending_verification`），把它们同时放进数据库会形成
"两处默认值"，日后只改一处就会静默分叉。

因此：

- **应用路径**（ORM / Repository 的 UPSERT）由模型默认值补齐，无需关心；
- **原生 SQL**（运维脚本、迁移、探针）必须自带这些列的值；
- 少数列**刻意提供**数据库默认值，因为它们在原生 SQL 里高频出现或是数据库自身的职责：
  `created_at` / `updated_at`（`now()`）、`id`（`gen_random_uuid()`）、
  `actions.requires_confirmation` / `can_undo`、`jobs.available_at`、
  `resource_versions.tags` / `publications.public_tags` / `publications.public_fields`
  （`'{}'`）、`runs.config_snapshot`（`'{}'`）、
  `retention_policies.applies_from`、`publications.published_at`。

写原生 SQL 时若漏列，数据库会以 `NotNullViolationError` **立即失败**，
而不是静默填入可能与应用不一致的值——这是期望的行为。

---

## 5 A7 完成判据自查

| 判据 | 结论 |
|---|---|
| `messages` 不可遗漏 | 已建（批三） |
| 累计正文的 `content_version` 与内容结构 `schema_version` 分开解释 | 见第 2 节"内容结构版本"行；两者不共用列 |
| Memory / 举报 / 保留策略 / 文件状态 / 摘要与联网来源的存储实体与交付阶段明确 | 全部"已建"，见第 2 节 |
| Publication 序号为资源内唯一；revision 必须归属同一 resource | `UNIQUE(resource_id, publication_no)` + 复合外键 |
| Action 幂等身份 NULL 语义明确；持久保存双版本 | `UNIQUE(actor_id, idempotency_key)` 全列非空；`expected_version` + `expected_acl_version` |
| DB 枚举与 API DTO 映射冻结 | 第 3 节 |
| `content_acl_epoch` 初始化责任明确，缺行不得当有效 0 | 批一迁移补种；`Repository` 读取时必须区分缺行与 0（阶段 B5） |
| 生产配置拒绝开发默认密钥；会话轮换 CHECK 兼容清理 | 见 `config.py` 与 `auth_sessions` 的单向蕴含约束 |
| 空库 base→head 升级成功 | `scripts/verify_fresh_migration.py` |
| Catalog 明确核对 UNIQUE / CHECK / FK / Partial Index / 触发器 | `tests/integration/test_catalog_contract.py` |
| 最小 PostgreSQL 测试 CI | `scripts/ci.ps1`（本地闸门）+ 仓库根 `.github/workflows/backend.yml`；用 pgvector 官方镜像，扩展建在测试库内，先空库 `upgrade head` 再 `alembic check` 再执行 unit / integration / concurrency |

### `.ps1` 必须是纯 ASCII

Windows PowerShell 会用**系统 ANSI 代码页**读取无 BOM 的脚本文件，
中文注释会变成乱码，甚至破坏字符串字面量的解析（`scripts/ci.ps1` 就因此无法执行）。
因此仓库内的 `.ps1` 一律只写 ASCII，中文说明放在 Markdown 里。
`.py` 不受影响（统一显式指定 UTF-8 读写）。
