# 后端与 Agent（autumn-prelude-backend）

同一个 FastAPI 项目承载业务、Agent 与后台作业。

实现依据：[后端与 Agent 设计](../docs/architecture/backend.md)、[接口契约](../docs/architecture/api-contract.md)与[数据库设计](../docs/architecture/database.md)。

形态：**模块化单体**。HTTP API、Agent Runtime、Worker 是三个入口，共享
`services` / `repositories` / `policies` 与基础设施适配器。

实施顺序见《秋序_v1.0_功能点实施顺序》与《v1.1 评审修订建议》。

当前进度：**A1–A7、B1–B12、C 核心并发、D 纯权限、E1–E8 应用服务、F1–F8 API、G1–G7 最小 Agent Runtime、H1–H8 Worker 与后台收敛完成**。
**28 张表**、4 个基线迁移 + B8 留言请求身份、E8 运行/来源代际与 F4 原稿动作枚举增量迁移；最新为 `a7c19e23b806`。
基线完成空库/并发验收；E 完成 29 项流程与 2 项结构基础核对、增量迁移回退/升级及 `alembic check`。
节点、验证结果与后续边界见 [DEVELOPMENT.md](DEVELOPMENT.md)。
数据契约的存储归属、枚举映射与有意偏离见 [DATA_CONTRACT.md](DATA_CONTRACT.md)。
身份入口、角色矩阵与来源/上下文失效契约见 [POLICY_CONTRACT.md](POLICY_CONTRACT.md)。
明确请求的发布/撤回事务见 [PUBLICATION_SERVICE.md](PUBLICATION_SERVICE.md)。
问答受理、角色限额、重试和固定额度桶见 [RUN_SERVICE.md](RUN_SERVICE.md)。
留言提交、一级回复、重复请求与错误边界见 [COMMENT_SERVICE.md](COMMENT_SERVICE.md)。
额度、文件、知识库、动作等待与正式结果提交见 [E_STAGE_SERVICES.md](E_STAGE_SERVICES.md)。
邮箱密码/会话认证已接入，说明见 [AUTH_CONTRACT.md](AUTH_CONTRACT.md)。已交付 API、SSE、版本/动作请求体及剩余入口见 [F_STAGE_API.md](F_STAGE_API.md)。LangGraph 与薄工具、权限/预算/等待恢复/来源提交见 [G_STAGE_AGENT.md](G_STAGE_AGENT.md)。Worker 启动、处理器、Postgres 检查点、故障回收与人工对账见 [H_STAGE_WORKERS.md](H_STAGE_WORKERS.md)。DeepSeek、百炼嵌入与 Tavily 已接入并通过真实 AI 链路基础验收，见 [PROVIDERS_ACCEPTANCE.md](PROVIDERS_ACCEPTANCE.md)；真实邮件、前后端完整联调及 I 阶段横切验收仍待完成。

当前没有 SMTP 配置项或 auth.email 发送处理器。配置文件位置、读取优先级和邮箱接入准备见 [EMAIL_CONFIGURATION.md](EMAIL_CONFIGURATION.md)；注册受理不表示邮件已经发出。

### 阶段 A 验收与评审修复

《秋序_实施顺序文档评审与 A 阶段验收》指出 4 个 P1 + 1 个 P2 缺陷、
D1–D12 文档问题，并判定"迁移安装与已有测试通过，但完整设计契约验收不通过"。
处置如下。

**缺陷修复**（已**折进重写后的迁移基线**，不再有单独的修复 revision）：

| 编号 | 缺陷 | 修复位置 |
|---|---|---|
| A-P1-1 | `publications.public_no` 被写成全局唯一 | `UNIQUE(resource_id, publication_no)` |
| A-P1-2 | `actions` 幂等键含可空列，NULL 让唯一约束失效 | 不可空 `UNIQUE(actor_id, idempotency_key)` + `parameters_hash` 判等 |
| A-P1-3 | 生产环境静默接受代码内的开发默认密钥 | `config.py`：生产必须**显式**提供且不得等于占位值 |
| A-P1-4 | publication 的 revision 未绑定同一资源 | 复合外键 `(resource_id, revision_id)` |
| A-P2-5 | 会话轮换 CHECK 与后继会话 `SET NULL` 冲突 | 单向蕴含，允许"后继已清理"的墓碑状态 |

**基线重写**：按 `docs/architecture/database.md`（物理建模说明）
把 stage A 的 schema 从 22 表扩到 **28 表**，并让所有既有表与该文档对齐
（`RevisionDTO` 级别的字段归属、`publications` 的类型化公开列 + `public_fields`
白名单、`knowledge_indexes` 与 `knowledge_chunks` 分层、`rate_limit_buckets`
改为 `(scope_hash, policy_key, window_start)` 复合主键、`quota_buckets` 删除静态限额等）。
因为**没有任何环境部署过**这批迁移，重写基线比堆叠一长串重命名 ALTER 更可读，
且重建空库不存在"偷改迁移后假称无漂移"的风险。四个批次与
`db/models/` 的四个模块一一对应。

**`alembic check` 不等于契约验收**（评审 D7）：autogenerate 不比较 CHECK 表达式，
也不把部分索引谓词变化算作差异。因此另有两层保护：

- `tests/integration/test_catalog_contract.py`：Catalog 名称集合双向比对
  （CHECK / UNIQUE / FK / 索引 / 触发器）、部分索引谓词逐条核对、
  向量列确认维度与扩展。
- 各 `test_*_constraints.py`：语义正反例（CHECK 表达式被 PostgreSQL 重写，
  无法逐字比较，行为测试才是语义证据）。

这套核对**当场抓到过真实漂移**：数据库里残留了被取代的旧约束名，
而 `alembic check` 全程报"无漂移"。

`alembic/env.py` 只排除 `db/external_tables.py` 里**显式登记**的外部表；
不用"排除所有反射表"的口径，否则应用表从 metadata 遗漏也不会被报告。

空库验证入口：`.\.venv\Scripts\python.exe scripts\verify_fresh_migration.py`
（重建专用库 → base→head → `alembic check` → 打印表/约束/触发器计数）。

**独立探针**：`.\.venv\Scripts\python.exe scripts\probe_constraints.py`
不复用测试套件，直接用原生 SQL 验证高风险约束（复合外键归属、`public_fields`
白名单、索引 scope 绑定、跨用户预留、部分唯一索引、复合主键、只追加表形态），
全部在事务内回滚。它已经抓到过一个测试没覆盖的**真实缺陷**：
`runs → conversations` 的复合外键列顺序写反，会拒绝所有合法的 run 插入。
因此新增高风险约束时，除了测试请同时补一条探针。

注意：可延迟外键（`resources.current_revision_id`、`runs` 的消息指针）
默认在**提交时**才校验，断言前需要 `SET CONSTRAINTS ALL IMMEDIATE`。

### 阶段 B 开工前已记录的前置约束

评审中不属阶段 A 但必须在对应阶段落实的条目，记在这里避免丢失：

- **B4**：`ON CONFLICT` 必须指定 `(user_id, idempotency_key)` 作为仲裁目标；
  "同 conversation 非终态唯一"是另一种冲突，不得被吞掉后当成命中幂等键。
- **B6**：`heartbeat` / `finish` 除匹配 `lease_token` 与 `status='running'` 外，
  还要校验数据库时间的租约有效期；过期 token 不得靠 heartbeat 复活。
  同时提供最小 `enqueue`，供 E 阶段使用。
- **B9/E1**：发布时锁内分别比较 `expected_version` 与 `expected_acl_version`。
- **B11**：`reclaim` 属阶段 **H8**（v1.0 文档误写为 F）。
- **C3/C5**：并发 Repository 用例各用独立事务；C3 的两个 run 用不同
  conversation 且额度 ≥ 2。C6 改为"同 run_id、amount 均为 1、bucket_id 不同"。
- **C8**：核心 5 例之外补扩展并发回归（最后一份额度争抢、charge/release/refund
  交错、内容 CAS、发布/撤回与旧预览、事件序号、父评论删除与回复创建、
  旧 lease 不得提交业务结果）。
- **H6/H7**：正式业务结果写入必须与 lease 校验、run generation 与权限复核
  **同一事务**；provider 的稳定 `logical_call_key` 与物理 attempt 编号分开。
- **G6**：step-up 轮换会话后，恢复需重新鉴权同一用户并受控原子关联新会话。
- 最小 PostgreSQL 测试 CI 应在本阶段先建立，而不是等到 I1。

## 依赖方向（硬约束）

```
api               -> services / auth / observability
agent.runtime     -> services / policies / providers（经工具适配）
workers.handlers  -> services / jobs.queue / agent.runtime
services          -> policies / repositories / providers / storage / jobs.queue
repositories      -> db / observability
jobs.queue        -> 数据库队列基础设施，不认识业务 handler
```

明确允许 `workers -> agent.runtime`；明确**禁止** `agent -> workers`、
`services -> agent`、`services -> workers`。阶段 I1 由 import-linter 把这三条
禁止依赖锁进 CI。

## 目录结构

```
backend/
├── alembic/                 # 迁移（env.py 只从 AUTUMN_DATABASE_URL 取 URL）
│   └── versions/            # 迁移脚本；命名约定由 db/base.py 固定
├── alembic.ini              # 必须保持 ASCII：configparser 按系统区域编码读取
├── pyproject.toml           # 依赖、pytest / ruff / mypy 配置（单一事实来源）
├── src/autumn_backend/
│   ├── app.py               # FastAPI 应用工厂与 lifespan
│   ├── cli.py               # 运维 CLI：check-db / config
│   ├── config.py            # 环境配置（AUTUMN_ 前缀，生产 fail-closed）
│   ├── api/ auth/ policies/ services/ agent/ repositories/
│   ├── jobs/ workers/ providers/ storage/
│   ├── db/                  # base（命名约定）/ mixins / naming / timestamps
│   │   ├── enums.py         # 受控词表（VARCHAR + CHECK，不用原生 ENUM）
│   │   ├── external_tables.py  # 允许存在的非应用表白名单（当前为空）
│   │   ├── session.py       # UoW
│   │   ├── health.py
│   │   └── models/          # identity(批一) / content(批二) / runtime(批三)
│   │                        # / knowledge(批四) —— 共 28 张表
│   └── observability/       # 结构化日志与脱敏
├── scripts/                 # bootstrap_db.py / verify_fresh_migration.py
│                            # patch_migration_triggers.py / probe_constraints.py
│                            # recreate_database.py / ci.ps1（阶段闸门）
├── DEVELOPMENT.md           # 本地节点实施进度；CI 位于仓库根 .github/workflows/backend.yml
├── DATA_CONTRACT.md         # v1 数据契约覆盖矩阵与枚举映射
└── tests/
    ├── unit/                # 无 I/O 纯单测
    ├── integration/         # 需要真实 PostgreSQL
    └── concurrency/         # 阶段 C 五个并发验收用例（硬闸门）
```

包根为 `src/autumn_backend`（`backend` 是目录名，不是包名）。

## 环境准备

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,agent]"
Copy-Item .env.example .env   # 按本机情况填写
.\.venv\Scripts\python.exe scripts\bootstrap_db.py   # 建应用库 + vector 扩展
```

`.env` 与 `.venv` 均已在 `.gitignore` 中忽略；密钥只通过环境变量传入。

本机开发实例：Docker 容器 `agent-postgres`（`pgvector/pgvector:pg17`）
映射在 `127.0.0.1:5442`，应用库 `autumn`，会话时区已锁定 UTC。

### 重新生成迁移基线

迁移是**手工整理过的**（触发器、函数创建、`content_acl_epoch` 补种都不是
autogenerate 的产物），因此只有在确实要重建基线时才走这条路径：

1. 逐个批次临时限制 `db/models/__init__.py` 的导入，使 autogenerate 只看到该批次的差异；
2. 每个批次：`alembic revision --autogenerate` → `alembic upgrade head`；
3. 用 `scripts/patch_migration_triggers.py` 补上触发器与函数创建；
4. 恢复完整的 `models/__init__.py`。

**注意**：Alembic 默认把整个 `upgrade head` 放在**一个事务**里，
因此任何一个批次的失败都会回滚全部批次——排查时不要只看最后一行输出。

**注意（`use_alter`）**：模型里声明 `use_alter=True` 的可延迟外键，
**不会**被它所在表的那条迁移发射；autogenerate 会把它生成到**下一个 revision**。
例如 `resources.current_revision_id` 的外键实际由批次三建立。
改这类外键（尤其是 `ondelete`）时必须**同时改内联声明与那条 `op.create_foreign_key`**，
否则只有元数据被改、库里仍是旧动作——`alembic check` 会发现这个漂移，
但用离线的 `alembic upgrade head --sql` 排查时看不到那条内联声明，容易误判。

## 常用命令

```powershell
# 阶段闸门（推荐）：在全新空库上跑迁移 + 测试 + 静态检查 + 约束探针
.\scripts\ci.ps1
.\scripts\ci.ps1 -SkipRebuild      # 复用已有 CI 库，调试更快

# 重建一个空库（默认只允许 *_ci / *_fresh / *_test 后缀，避免误伤开发库）
.\.venv\Scripts\python.exe scripts\recreate_database.py autumn_fresh

# 测试：unit 无需数据库；integration / concurrency 需要 AUTUMN_TEST_DATABASE_URL
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q -m unit

# 静态检查
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy

# 迁移
.\.venv\Scripts\alembic.exe revision --autogenerate -m "message"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe check          # 模型与迁移不得漂移（不覆盖 CHECK 与触发器）
.\.venv\Scripts\python.exe scripts\verify_fresh_migration.py   # 空库 base→head + check

# 运维自检
.\.venv\Scripts\python.exe -m autumn_backend.cli config
.\.venv\Scripts\python.exe -m autumn_backend.cli check-db
```

## 配置要点

| 变量 | 说明 |
|---|---|
| `AUTUMN_ENVIRONMENT` | `local` / `dev` / `test` / `prod`；生产缺少合规密钥时启动即失败 |
| `AUTUMN_DATABASE_URL` | PostgreSQL DSN；`postgresql://` 会自动补 `+asyncpg` |
| `AUTUMN_SESSION_SECRET` / `AUTUMN_CSRF_SECRET` | 生产必须 ≥32 字符 |
| `AUTUMN_COOKIE_SECURE` / `AUTUMN_COOKIE_NAME` | 生产强制 `Secure` + `__Host-` 前缀 |
| `AUTUMN_QUOTA_TIMEZONE` | 每日额度切分时区，默认 `Asia/Shanghai`；数据库时间统一 UTC |

数据库连接的会话时区被强制为 UTC（`connect_args.server_settings.timezone`）。

## 数据模型约定（A2 起）

| Mixin | 列 | 语义边界 |
|---|---|---|
| `UUIDPrimaryKey` | `id UUID` | 仅 UUID 主键表；`Setting` 等 str 主键表**不**套用 |
| `Versioned` | `version BIGINT NOT NULL` | **只服务私人内容/metadata 的乐观并发** |
| `Timestamped` | `created_at` / `updated_at` timestamptz | 时间统一 UTC |
| `SoftDelete` | `deleted_at` / `archived_at` | 两个时刻互相独立；`SoftDeleteState` 三态 |

**`version` 与 `acl_version` 完全解耦**：`Versioned` 故意**不**提供 `acl_version`。
publish / revoke / 软删除 / 恢复只递增 `acl_version`，不伪造 `version` 变化——
需要它的表在阶段 A5 显式声明该列。全站 `settings.content_acl_epoch` 同理。

**CAS 不在 Mixin 里**：`WHERE id=? AND version=? RETURNING` 属于阶段 B2 的
Repository 能力。Mixin 只保留 `version_matches()` 让 service 表达"我基于哪个版本判断"；
真正的并发仲裁只有数据库语句能给。

**`updated_at` 双重保障**：ORM 的 `onupdate=now()` + PostgreSQL `BEFORE UPDATE`
触发器（`db/timestamps.py`）。触发器函数由迁移创建，运行期按表挂载；
`timestamped_tables()` 为迁移脚本提供需要挂触发器的表清单。

**对象名统一由 `db/naming.py` 生成**：Partial Unique Index 与命名 CHECK 必须显式命名
（PostgreSQL 不会替它们取名），名字在两处各写一遍就会导致 `alembic check` 漂移。
注意 `CheckConstraint(name=...)` 只写**标签**（`"role_valid"`），完整名字
`ck_<table>_<label>` 由命名约定补齐；`check_constraint_name()` 只用于断言与核对。

## 内容与发布约定（A5 起）

| 表 | 关键不变量 |
|---|---|
| `resources` | `version`（私人内容）与 `acl_version`（可见性）解耦；私密备注默认不公开，只有明确选中 note 投影才复制 |
| `resource_versions` | 不可变：编辑新增行，已发布版本行只读；`(resource_id, revision_no)` 唯一 |
| `publications` | **单资源只有一个现行公开版本**（`resource_id WHERE revoked_at IS NULL`） |
| `comments` | `(author_id, client_id)` 唯一 + 原始 `request_hash` 判等；一级回复与父未删除在锁内校验，同资源由复合 FK 和 Repository 共同校验 |
| `knowledge_indexes` | `scope='public'` 必须绑定同资源、同 revision 的 publication；ready 且 active 才能使用 |
| `run_sources` | AppendOnly：只记录，不提供 update/delete；绑定 acl_version 与片段位置 |

**公开索引的来源绑定由结构约束保护**：

```
(scope = 'public') = (publication_id IS NOT NULL)
(resource_id, revision_id, publication_id) -> publications(resource_id, revision_id, id)
NOT is_active OR status = 'ready'
```

公开分块必须从白名单投影文本构建，这仍需 E5 的应用服务与泄漏回归验证，
不能只靠外键证明。向量在 `knowledge_chunks`，维度 1024；换维度必须写迁移。

**父评论跨行不变量由 Repository 承担**（阶段 B8）：`CHECK` 不能跨行查询，
普通 FK 只能保证父行存在，因此"父存在、父无 parent、`resource_id` 一致、父未删除"
必须在事务内锁定父行校验。集成测试把这个边界写成了可执行文档
（`test_parent_invariant_is_not_enforced_by_database`）。

## Run / 配额 / Job / 审计约定（A6 起）

| 表 | 关键不变量 |
|---|---|
| `runs` | `UNIQUE(user_id, idempotency_key)`；**同 conversation 只有一个非终态 run** |
| `runs.next_event_seq` | 事件序号的**唯一**分配器，初值 1 → 首次分配得到 1 |
| `run_events` | `PRIMARY KEY(run_id, seq)`；不接受外部指定 seq |
| `quota_buckets` | `UNIQUE(user_id, window_start)`；`used>=0`、`reserved>=0`；锁内与当前配置限额比较，不存静态 limit_value |
| `quota_reservations` | `UNIQUE(run_id)`；`amount = 1`；状态机不倒退 |
| `jobs` | 幂等键全局唯一；SKIP LOCKED 领取；有效期、running 状态与 token 共同保护 heartbeat/finish |
| `provider_calls` | `UNIQUE(logical_call_key, attempt_no)`；外部幂等键不随 attempt 改变；unknown 费用未知 |
| `audit_events` | `before_version`/`after_version` **专指 `resources.version`**，ACL 前后值进 `metadata_json` |
| `actions` | 不可空 `(actor_id, idempotency_key)` + parameters_hash；保存内容与 ACL 双版本 |

**非终态谓词由枚举派生**，不手写字符串：

```python
Index(
    "uq_runs_conversation_id_non_terminal",
    "conversation_id",
    unique=True,
    postgresql_where=text(in_predicate("status", NON_TERMINAL_RUN_STATUSES)),
)
```

索引谓词与 `RunStatus` 不可能各自漂移；单元测试另外断言非终态与终态集合互补、
并集等于整个枚举。

**`actions` 的状态与时刻用包含式约束**，不是双向等式：

```
status NOT IN ('ready', 'running', 'succeeded') OR confirmed_at IS NOT NULL
```

双向等式在这里是错的——执行态同时需要 `confirmed_at` 与 `executed_at`，
写成等式会直接拒绝"确认后执行"的正常流程。包含式同样拦住半截转换
（`pending` 带上任何转换时刻都会被拒绝）。

## 测试分层的硬规则

- `tests/unit`：只放无 I/O 的测试（policies、cursor、hash、纯状态机、配置校验）。
- `tests/integration` / `tests/concurrency`：**必须**使用真实 PostgreSQL。
  **禁止**用 SQLite 或 Mock Repository 替代——无法证明 `ON CONFLICT`、
  `FOR UPDATE`、`SKIP LOCKED`、Partial Unique Index 与隔离级别。
- 真实数据库用例通过 `AUTUMN_TEST_DATABASE_URL` 提供独立测试库；
  未设置时这些用例显式 skip，而不是退化成假通过。

```powershell
$env:AUTUMN_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5442/autumn_test_example"
.\.venv\Scripts\python.exe -m pytest -q
```

集成测试夹具为每个用例开一个外层事务并在结束时回滚，因此负向用例
（故意触发约束违例）不会污染数据库。**注意**：ORM 的 `validate_strings=True`
会先在 Python 侧拦下非法枚举值，所以验证数据库 CHECK 的用例必须用**原生 SQL**
绕过它——应用层防线与数据库最后防线要分别证明。

## 已知环境事项

- `alembic.ini` 必须保持纯 ASCII：Windows 上 `configparser` 按 GBK 读取会直接抛
  `UnicodeDecodeError`。中文说明写在 `alembic/env.py`。
- 迁移**不会**自动格式化：`console_scripts` 钩子会以 `Could not find entrypoint`
  失败。生成后手动执行 `python -m ruff format alembic/versions`。
- 触发器 DDL 必须**逐条**执行：asyncpg 的预编译语句不接受一次多条命令
  （`cannot insert multiple commands into a prepared statement`），
  因此 `db/timestamps.py` 返回语句列表而不是一段脚本。
- `op.create_table` 会触发 `Table.after_create` 事件，事件与迁移里的显式调用
  **都会**挂触发器，因此挂载语句必须幂等（`DROP TRIGGER IF EXISTS` 在前）。
- `gen_random_uuid()` 需要 PostgreSQL 13+（内置 `pgcrypto` 能力），当前实例为 17。
- 本机另有一个 PostgreSQL 18 实例在 5432，但未安装 pgvector 且密码未知；
  请使用容器实例（5442）。
- `pgvector` 是**核心依赖**（A5 起），不是可选 extras：`knowledge_chunks.embedding`
  是 `Vector(1024)` 列。
- 模型里名为 `text` 的列会遮蔽 `sqlalchemy.text`，因此该类体内的部分索引谓词
  必须写 `sa.text(...)`；测试断言谓词编译结果是 SQL 表达式而非字符串，
  否则索引会退化成全表索引。
- 测试里不能在绑定参数上直接加类型转换（`:name::regclass` 会语法报错，
  asyncpg 不支持），改为 JOIN `pg_class` 按 `relname` 查。
- 不要用 `session.scalar()` 执行带 `RETURNING` 的 `UPDATE`：请用
  `session.execute(...)` 再取 `result.scalar_one()`，否则在 "仅 DML" 预编译下失败。
- 现有基线已冻结。改模型后新增增量迁移，验证空库与已有数据升级；
  不修改已应用的迁移来掩盖差异。`alembic check` 之外仍需验证 CHECK 和触发器。
