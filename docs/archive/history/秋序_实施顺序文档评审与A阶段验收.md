# 秋序实施顺序文档评审与 A 阶段验收

日期：2026-10-05。仓库：mosterakie/autumn-prelude。验收代码：`aac49678750d096a28b3e43c68e3b92927b276c5`。

## 1. 结论

实施顺序的总体方向正确：保留模块化单体、短事务、数据库仲裁、公开投影隔离，先验证 Repository 并发，再开发业务 API 和 Agent。无需重新设计整体分层。

但这份文件还不能直接作为冻结的验收标准。它遗漏了部分后续功能所依赖的数据契约，也保留了两份原始架构文档中的若干歧义。上一轮《秋序后端架构评审与知识点详解》指出的权限版本、租约 fencing、供应商逻辑身份等问题，并未完整纳入本次顺序表。

**A 阶段的迁移安装和已有测试通过；A 阶段的完整设计契约验收不通过。** 真实 PostgreSQL 上新增的五项边界检查均复现了问题。尤其是发布序号、Action 去重与生产密钥校验，需要在进入 B 阶段前修正。

本次只拉取、评审和验收，没有修改业务代码、迁移或原始桌面文档，也没有启动 B 阶段开发。前端已有的自动生成类型文件差异保留。

## 2. 文档中应保留的设计

| 设计 | 判断 |
| --- | --- |
| Model → 迁移 → Repository → 真实 PostgreSQL 并发 → Policy → Service → API → Agent → Worker | 合理；建议让并发核心测试更早落地 |
| UoW 统一事务，Repository 禁止 commit | 合理；一个 Service 可包含多个短事务 |
| UNIQUE / UPSERT / CAS / 行锁承担并发仲裁 | 合理；不能只靠前置 SELECT |
| 内容 version 与 ACL version 分离 | 合理；可见性变更需要独立冲突检查 |
| ActorContext 服务端构造，恢复时重新鉴权 | 合理；还需明确会话轮换后的恢复方式 |
| 公开索引从公开投影构建 | 合理；外键与 CHECK 只保证部分结构，内容构建还需要 Service 验证 |
| unknown 不视为零成本失败，外部 I/O 在事务外 | 合理；还需规定对账和稳定逻辑调用身份 |

SQLAlchemy 官方同样要求并发任务各用独立 AsyncSession。[异步会话说明](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks)。

## 3. 实施顺序文档的问题与修订建议

### D1. A 阶段表清单不完整

A6 没有列出 `messages`，但 E2 要插入消息、F7 要持久化正文；远端实现已正确补入消息表。应修正文档，避免按清单验收时遗漏。

B10 使用 Memory，F/E 使用文件生命周期，前端和接口契约还包括举报、手动记忆、保留策略、会话摘要；顺序表没有交代这些状态的数据归属。建议在 A 阶段增加一次“v1 数据契约覆盖检查”，对每项标明：已有表、已有 JSON 结构、需要新迁移、明确延期。不是要求机械地增加所有表，而是必须明确存在哪里、由谁维护、何时交付。

文件 `staged / ready / pending_delete` 的状态尤其不能只写在 E5 的流程文字里。需要明确对应的可变文件对象记录，避免反复修改声明不可变的资源版本。

### D2. C6 与 `CHECK(amount=1)` 冲突

首版只允许每次预留 1 次，amount=2 本身不是合法请求。因此“同 run_id、不同 amount，一方成功另一方一定 ConflictError”不是有效的并发语义证明。

建议将 C6 改为：**同 run_id、amount 都为 1、但 bucket_id 不同，语义不一致的一方收到领域冲突，并且不能增加第二个桶的计数。** amount≠1 单独做输入校验与数据库 CHECK 的负向用例。

C3/C5 中的并发 Repository 验收应使用独立事务；C3 的两个 run 使用不同 conversation，桶额度至少为 2。端到端受理还受“普通用户最大并发 1”限制，不能把 Repository 测试预期直接套到完整 API。

### D3. E1 没有比较 ACL 版本

E1 只写 expected_version + revision_id，遗漏上一轮评审及当前 API 契约已要求的 expected_acl_version。

两次预览的内容版本都为 5：A 撤回使 ACL 从 2 变成 3，B 仍根据旧 ACL=2 发布。行锁只能让动作串行执行，不能识别 B 的授权已过期。

发布、撤回、删除等权限操作的 Action 应保存两个版本；执行时在锁内分别比较。不得只用一个 target_version 同时表示内容版本或 ACL 版本。

### D4. 归档会影响公开可见性

B10 只把归档归入内容 version 的递增操作，但公开读取又要求 archived_at 为空。归档已经公开的资源，会改变访问权限。

应明确：私人 metadata 变化递增 version；影响可见性的归档、撤回、删除等同时按规则递增 acl_version 和 content_acl_epoch。是否撤销 publication，以及取消归档后是否需要重新发布，要定义一致语义，不能无意中恢复旧授权。

### D5. Lease 保护不能只落在 finish

B6 的 token + running 只拒绝已被新 worker 替换的旧 token；如果要求“时间到点租约立即失效”，还需要数据库时间的有效期判定。过期 token 不应通过 heartbeat 重新取得提交资格。

H6 还需要规定：**检查租约与提交正式业务结果必须在同一个短事务内完成**。只在最后 finish 检查会出现“旧 worker 的业务结果已提交，finish 才被拒绝”的漏洞。并应校验 run generation / 当前权限，保证旧上下文不能提交。

由于外部请求已经发出后无法保证撤销，租约保证的是后续新副作用与数据库结果的提交资格，而不是外部 exactly-once。

### D6. 队列依赖和 CI 实现时机需要前移

E1/E2/E5 已需要 enqueue，H1 才开始实现 jobs.queue。应在 B6 提供最小 enqueue / claim / heartbeat / finish 及薄队列入口；H 阶段再完成 worker 调度和 handlers。

B6 写“reclaim 留到阶段 F”，而 F 是 API、实际 reclaim 在 H8，属于明确的章节引用错误，应改为 H8。C 阶段可通过受控测试夹具模拟重新领取后的 token，生产回收器随后实现。

C 要求“五个用例在 CI 跑绿”，I 才添加 CI 过晚。应在 A/C 先提供最小 PostgreSQL 测试 CI，I 再汇总 Agent / Worker / 跨用户测试和最终门禁。核心 run/quota/job Repository 完成后立即跑核心并发检查，不必等所有分页、审计、内容方法写完。

### D7. `alembic check` 不代表所有约束已被证明

它复用 autogenerate 的能力，不能代替检查 CHECK 表达式、触发器函数和业务语义。当前 env.py 还会跳过所有“数据库有、metadata 没有”的表，应用表意外遗漏也可能不被报告。

完成判据应同时包括：空库从 base 升到 head；迁移链正确；自动比较范围内无差异；PostgreSQL catalog 中约束/索引/触发器存在；关键约束正反例通过。外部表应使用明确允许列表或独立 schema，而不是一概排除。[Alembic autogenerate 的检测范围与限制](https://alembic.sqlalchemy.org/en/latest/autogenerate.html#what-does-autogenerate-detect-and-what-does-it-not-detect)。

### D8. Policy 的零 I/O 范围前后矛盾

D5 允许 loader 装配多表 facts，D 完成判据却写整个 policy 层不能有任何 I/O / ORM。应改为：**判定函数和核心 actor/facts/decision 类型零 I/O**；可选 loader 只读装配，通过约定接口调用 Repository。这样才能同时满足两条规则。

### D9. 幂等重放必须有当前权限边界

E2 先检查当前 cooldown / quota 再找到历史幂等结果，可能让已成功请求因为后来配置降低变成失败。

建议顺序：认证与基本归属校验 → 查幂等身份并比较语义 → 若为重放，按当前权限返回仍可见的原结果 → 只有新请求检查受理冷却、速率、并发和日额度。账号禁用、权限撤回后不得通过重放读回私人正文。

B4 的 ON CONFLICT 应指定 `(user_id, idempotency_key)` 仲裁目标；同 conversation 非终态唯一约束是另一种冲突，不应被吞掉后当成找到了相同幂等键。

### D10. Provider attempt 不能代替逻辑调用身份

`UNIQUE(job_id,purpose,attempt_no)` 只能避免同一 attempt 重复记录。队列重试增大 attempt_no 后，用它生成新的外部幂等键会允许再次执行同一笔操作。

应区分稳定 logical_call_key 和物理尝试编号；同一逻辑调用重送时复用外部幂等键。unknown 先对账，受控转为已确认状态，并只结算一次。job_id 可空时，还要明确 run-only 调用如何去重。

### D11. TOTP 轮换与 Run 恢复需要配套

F2 要轮换会话，G6 又只从 run 原来绑定的 auth_session_id 恢复。step-up 后旧会话失效，若不补充受控重新绑定，waiting_auth 的任务可能无法继续。

应定义：用当前 Cookie 重建同一用户身份，重新验证 run/action 归属与权限，再原子关联新 auth_session_id；checkpoint 不能自行替换身份。并明确管理员验证过期、账号禁用、权限撤回对排队、等待和运行中任务的不同处理。

### D12. 并发用例覆盖需要扩充

五个核心用例应保留，但不足以证明全部 v1 不变量。至少补充：最后一份额度被并发争抢；charge/release/refund 交错；reserve 已结算记录的重放；内容 CAS 两个写者；发布/撤回与旧预览交错；事件序号并发；父评论删除与回复创建；旧 lease 不能提交正式业务结果。

PostgreSQL 的 CHECK 不适合跨行约束，跨对象归属可用复合 FK，不能表达的规则由锁和事务承担；不能把“存在 FK”误认为“引用同一个业务对象”。[PostgreSQL 约束说明](https://www.postgresql.org/docs/17/ddl-constraints.html)。

## 4. A 阶段已执行的验收

| 项目 | 本次结果 |
| --- | --- |
| 远端拉取 | fast-forward 至 aac4967，无合并冲突 |
| 数据库 | 127.0.0.1:5442，PostgreSQL 17.11 |
| 隔离 | 新建本次专用验收库，未对原 autumn 业务库运行迁移/清空操作 |
| pgvector | 验收库显式启用 0.8.6，符合 README 的预置要求 |
| base → head | 三个迁移升级成功，head=4c4f5ba032f4 |
| alembic check | 通过，No new upgrade operations detected |
| 数据表 | 22 张应用表 |
| Catalog | public schema 共 188 项约束（含 100 CHECK、47 FK）；87 个索引；22 个用户触发器；统计包含 Alembic 版本表 |
| CHECK 名称对比 | metadata 声明的 CHECK 没有缺失；不等同于表达式语义全部正确 |
| 原有 pytest | 364 passed：226 unit、138 integration；无数据库用例跳过 |
| Ruff / 格式 | 通过；格式检查覆盖 43 个文件 |
| Mypy strict | 通过，27 个源文件 |
| 阶段 C | Repository 并发用例尚未实现，不属于 A 已通过的证明 |

第一次在无 vector 扩展的空库迁移会报 vector 类型不存在，这是环境预置条件，并非本次单独认定的迁移缺陷。启用扩展后迁移与检查成功。

第一次测试时验收脚本设置 AUTUMN_ENVIRONMENT=test，使一项默认环境断言失败；移除该脚本环境覆盖后，原有 364 项全部通过。格式扫描初次遇到 Windows 临时缓存目录权限问题，改为显式源目录后通过。报告最终结果不把这两类验收工具问题算成产品缺陷。

依赖按 pyproject.toml 的版本区间新装于隔离环境。实际版本保存在证据目录，不假定与提交作者环境完全相同。当前仍有第三方 TestClient 弃用提示，未影响通过结果。

## 5. 五项补充验收发现

P1 表示需要在进入下一阶段前修复；P2 表示需要补齐生命周期或数据边界，不宜留到最终验收才决定。

### A-P1-1. 发布序号错误地采用全局唯一

位置：`backend/src/autumn_backend/db/models/content.py:247`，对应第二批迁移同样错误。

模型注释、B9 和仓库 database.md 都把 public_no 定义为资源内序号，但实际使用 `UNIQUE(public_no)`。

复现：文章 A 发布第一版 public_no=1；文章 B 也发布自己的第一版 public_no=1。第二次插入被 `uq_publications_public_no` 拒绝。

建议改为 `UNIQUE(resource_id, public_no)`，保留“单资源当前 publication 唯一”的部分索引，并增加“不同资源同序号允许、同资源重复序号拒绝”的成对测试。

### A-P1-2. 可空 Action 幂等键不能保证去重

位置：`backend/src/autumn_backend/db/models/runtime.py:666`、`:692`。

复现：同 run、同 kind、同 args_hash、同 target_version，target_id=NULL 的设置动作连续插入两次，数据库保留 2 行。

PostgreSQL 默认唯一约束允许多条包含 NULL 的键；因此当前约束不能保证这类动作幂等。[NULL 与 UNIQUE 的行为](https://www.postgresql.org/docs/17/ddl-constraints.html#DDL-CONSTRAINTS-UNIQUE-CONSTRAINTS)。

建议优先与 API 契约统一为不可空的 `(actor_id,idempotency_key)`，另存参数摘要以判等；如保留复合语义键，则须明确定义 target_type、可空字段与 `NULLS NOT DISTINCT` 的语义。不要只用 ORM 前置 SELECT 修补。

### A-P1-3. 生产配置接受开发默认密钥

位置：`backend/src/autumn_backend/config.py:62`、`:104`。

复现：未提供 SESSION_SECRET / CSRF_SECRET，仅指定 prod + Secure + __Host- Cookie，Settings 构造成功，仍使用代码内的开发默认密钥。现有校验只检查长度，而开发默认值已经超过 32 字符。

这不满足 A1/README 宣称的生产 fail-closed。

建议生产环境要求显式密钥，拒绝开发占位值；如支持 secret file 或其他配置来源，也必须经过同样校验。回归测试覆盖“两个都未提供、只缺一个、占位值、有效显式值”，并确认错误日志不打印密钥。

### A-P1-4. Publication 与 revision 的资源归属没有共同约束

位置：`backend/src/autumn_backend/db/models/content.py:213`、`:216`。

复现：publication.resource_id 指向文章 A，但 resource_version_id 指向文章 B 的版本；插入成功。两个独立 FK 只证明两行存在，没有证明属于同一资源。原 database.md 已要求严格的复合引用。

建议建立 resource_versions 的配套唯一键和 `(resource_id,resource_version_id)` 复合 FK；KnowledgeIndex 的 resource / version / publication 及 owner 归属也应逐项确定数据库保护与事务校验责任。公开读取不能只检查 publication.resource_id 对应资源的权限，却取另一个资源的原稿。

### A-P2-5. 会话轮换 CHECK 与 SET NULL 冲突

位置：`backend/src/autumn_backend/db/models/identity.py:174`、`:196`。

复现：旧会话记录 rotated_at 并指向新会话；删除新会话时，FK 的 SET NULL 会清空 replaced_by_session_id，旧 rotated_at 仍保留，触发 rotation_marker_consistent，删除被拒绝。

建议允许“已轮换但后继已清理”的墓碑状态，例如保留 rotated_at，约束仅要求有后继时必须有轮换时刻；或统一采用保留会话链的生命周期策略。不能在清空后继引用时把旧会话误恢复成可用。增加轮换链清理测试。

## 6. A 阶段的其他缺口

| 缺口 | 判断 / 后续安排 |
| --- | --- |
| storage 包 | A1 清单与 README 都列出，但本提交没有实际 storage 包，应补骨架或明确后移 |
| content_acl_epoch 初始化 | 新库没有该 key；必须明确 bootstrap/迁移/Repository 首次创建责任，不能把缺行当成有效的 0 静默继续 |
| 公开索引“结构上绝不污染” | 当前 CHECK 只约束指针种类，不能证明 text 来自白名单公开投影；后续要测真实构建逻辑，不能只用手工安全测试数据得出全面结论 |
| 状态与既有 API 契约不一致 | Run 的 pending 与 DTO queued；缺少显式 waiting_approval/waiting_auth/cancelling；Comment 仅 visible/hidden，契约需要待审核流程；Action 两套状态集合。要冻结明确映射或改模型与迁移 |
| 留言板资源 | comments.resource_id 不可空，而公开留言板允许无文章关联；需要明确特殊资源还是可空模型，不能让 API 临时猜测 |
| 消息与来源 | content_version 是累计正文版本，不应混为用户消息结构版本；联网来源、消息展示状态与双版本 Action 的持久化需要补齐 |
| 支撑功能 | Memory、举报、保留策略、文件状态、摘要/来源失效路径均需逐项明确，不把当前 22 表等同于完整 v1 数据契约 |

这些缺口有的可以通过明确的已有实体映射解决，不必一律新建表；但需要在依赖该功能的阶段开始之前形成一致文档与测试。

## 7. 建议的后续顺序

1. 修正实施顺序文件，明确 A 的核心 22 表与完整 v1 覆盖边界，补齐接口枚举、状态和双版本语义。
2. 修复上述四个 P1 与会话轮换清理问题；已应用的迁移用新增修复 revision，不直接改旧迁移来制造“无漂移”。
3. 将补充验收转为仓库回归测试，重新跑新库升级、Catalog、原有测试与新增测试；届时再确认 A 阶段通过。
4. B1–B7 做 UoW / Run / Quota / Job / Provider 核心；立即执行 C 的核心并发闸门。
5. 完成其余内容 Repository 与扩展并发检查，进入 Policy / Service / API / Agent / Worker。部署继续暂缓。

## 8. 证据

同目录的 `stage-a-audit/` 保留以下文件：

- `checks.json`、`migration-upgrade.txt`、`alembic-check.txt`：迁移及检查结果。
- `pytest.txt`、`pytest.xml`：364 个已有用例的本次运行结果。
- `ruff.txt`、`ruff-format.txt`、`mypy.txt`：静态检查。
- `additional-probes.json`：五项补充验收的预期与实际结果。
- `catalog.json`：表、约束、索引、触发器与初始 epoch 的实测信息。
- `dependencies.json`：本次隔离验收环境的依赖版本。

证据不包含 Cookie、生产密钥或业务库内容。补充测试均在事务结束后回滚。本次专用验收库完成后清理，原业务库保留。

清理说明：专用数据库已确认应用行数为 0 后删除，证据见 cleanup.json。自动审批拒绝删除两个 pytest 临时缓存目录，原因是删除需审批、当前策略禁止此类审批申请；这些目录保留，不算产品缺陷。
