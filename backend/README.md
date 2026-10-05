# 后端与 Agent（autumn-prelude-backend）

同一个 FastAPI 项目承载业务、Agent 与后台作业。

实现依据：[后端与 Agent 设计](../docs/architecture/backend.md)、[接口契约](../docs/architecture/api-contract.md)与[数据库设计](../docs/architecture/database.md)。

形态：**模块化单体**。HTTP API、Agent Runtime、Worker 是三个入口，共享
`services` / `repositories` / `policies` 与基础设施适配器。

实施顺序见《秋序_v1.0_功能点实施顺序》。

当前进度：**阶段 A6 已完成**——阶段 A（地基与数据库契约）全部完成：
22 张表、3 个迁移版本、`alembic check` 无漂移。

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
│   │   ├── session.py       # UoW
│   │   ├── health.py
│   │   └── models/          # identity（A3）/ content（A5）/ runtime（A6）│   └── observability/       # 结构化日志与脱敏
├── scripts/                 # 运维脚本（bootstrap_db.py）
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
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env   # 按本机情况填写
.\.venv\Scripts\python.exe scripts\bootstrap_db.py   # 建应用库 + vector 扩展
```

`.env` 与 `.venv` 均已在 `.gitignore` 中忽略；密钥只通过环境变量传入。

本机开发实例：Docker 容器 `agent-postgres`（`pgvector/pgvector:pg17`）
映射在 `127.0.0.1:5442`，应用库 `autumn`，会话时区已锁定 UTC。

## 常用命令

```powershell
# 测试：unit 无需数据库；integration / concurrency 需要 AUTUMN_TEST_DATABASE_URL
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q -m unit

# 静态检查
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy

# 迁移（A3/A4 起产生实例迁移）
.\.venv\Scripts\alembic.exe revision --autogenerate -m "message"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe check          # 模型与迁移不得漂移

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
| `Versioned` | `version INT NOT NULL` | **只服务私人内容/metadata 的乐观并发** |
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
| `resources` | `version`（私人内容）与 `acl_version`（可见性）解耦；`private_note` 绝不公开 |
| `resource_versions` | 不可变：编辑新增行，已发布版本行只读；`(resource_id, version_no)` 唯一 |
| `publications` | **单资源只有一个现行公开版本**（`resource_id WHERE revoked_at IS NULL`） |
| `comments` | 幂等身份 `(author_id, client_id)` 唯一；父评论跨行不变量**不在 DB 层** |
| `knowledge_indexes` | **公开索引只能来自公开投影**：CHECK 把 `corpus_kind` 绑死到具体外键列 |
| `run_sources` | AppendOnly：只记录，不提供 update/delete；绑定 acl_version 与片段位置 |

**公开索引的防污染是结构性的**，不靠约定：

```
corpus_kind = 'private' AND source_kind = 'resource_version'
    AND resource_version_id IS NOT NULL AND publication_id IS NULL
OR
corpus_kind = 'public'  AND source_kind = 'publication'
    AND publication_id IS NOT NULL AND resource_version_id IS NULL
```

于是"复用私人 chunk 再加 public 标记"无法被表达。向量列维度 1024 由
`EMBEDDING_DIMENSIONS` 固定，`embedding_dimensions` 列另有 CHECK 与之对齐——
换维度必须写迁移。

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
| `quota_buckets` | `UNIQUE(user_id, window_start)`；`used>=0`、`reserved>=0`、`used+reserved<=limit_value` |
| `quota_reservations` | `UNIQUE(run_id)`；`amount = 1`；状态机不倒退 |
| `jobs` | lease 字段与状态一致；活跃去重部分唯一索引；`ix_jobs_status_available_at` 对齐 claim 顺序 |
| `provider_calls` | `UNIQUE(job_id, purpose, attempt_no)`；`unknown` 是一等状态 |
| `audit_events` | `before_version`/`after_version` **专指 `resources.version`**，ACL 前后值进 `metadata_json` |
| `actions` | 幂等身份 `(run_id, kind, target_id, target_version, args_hash)`；状态与转换时刻一致 |

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
(confirmed_at IS NULL) = (status NOT IN ('confirmed', 'executed'))
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
$env:AUTUMN_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5442/autumn"
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
- `pgvector` 是**核心依赖**（A5 起），不是可选 extras：`knowledge_indexes.embedding`
  是 `Vector(1024)` 列。
- 模型里名为 `text` 的列会遮蔽 `sqlalchemy.text`，因此该类体内的部分索引谓词
  必须写 `sa.text(...)`；测试断言谓词编译结果是 SQL 表达式而非字符串，
  否则索引会退化成全表索引。
- 测试里不能在绑定参数上直接加类型转换（`:name::regclass` 会语法报错，
  asyncpg 不支持），改为 JOIN `pg_class` 按 `relname` 查。
- 不要用 `session.scalar()` 执行带 `RETURNING` 的 `UPDATE`：请用
  `session.execute(...)` 再取 `result.scalar_one()`，否则在 "仅 DML" 预编译下失败。
- 改了模型约束后必须回退并**重新生成**对应迁移；直接改已应用的迁移会让
  `alembic check` 与实际库不一致。
