# H 阶段：后台任务与故障收敛

H1–H8 已完成。Worker 与 HTTP 进程分开启动，仍共享同一个后端项目、数据库和业务服务。任务可以至少执行一次，正式结果只能由当前租约、当前代际和当前授权联合提交。未知的供应商结果保留待对账，不自动重发。

## 启动与装配

在 backend 目录安装 `.[dev,agent,providers]`。数据库及对象目录使用现有 `AUTUMN_DATABASE_URL`、`AUTUMN_STORAGE_ROOT`；开发验证使用 5442 的独立测试库。启动命令会执行任务，不是只读检查，执行前应确认指向需要处理的环境。

```powershell
python -m autumn_backend.workers --once
python -m autumn_backend.workers
```

默认装配文件、人工确认的数据库动作、取消、索引失效清理和会话派生摘要清理。没有配置真实模型、Embedding 或邮件适配器时，其余任务保持排队。不会伪造回复、向量或邮件发送成功。

后续已补齐真实 AI 装配：配置百炼后注册知识任务，配置 DeepSeek、百炼和已初始化的 PostgreSQL saver 后注册 Run 任务；Tavily 配置后提供站长联网工具。缺少必要的嵌入端口或检查点时模型 Worker 启动失败，不能提供伪造回复。具体配置和验收边界见 [PROVIDERS_ACCEPTANCE.md](PROVIDERS_ACCEPTANCE.md)。SMTP 适配与固定邮件处理器也已补齐，授权码非空时注册，配置和真实收件验证边界见 [EMAIL_CONFIGURATION.md](EMAIL_CONFIGURATION.md)。

完整 Agent 工厂通过可信本地 `--factory module:factory` 指定，工厂是返回 Worker 的异步上下文管理器。它负责端口连接的生命周期，并调用 `workers.bootstrap.configured_worker(uows, storage, knowledge=..., model=..., saver=...)`；model 必须同时配置 knowledge 和持久 saver。这个入口不接受 HTTP 或模型传入的模块路径。

官方 PostgreSQL saver 在独立 `autumn_checkpoints` schema 保存框架表，应用 Alembic 仍管理 public schema 的 28 张业务表。首次使用需单独、显式执行：

```powershell
python -m autumn_backend.workers --setup-checkpoints
```

普通启动只校验框架迁移版本，不自动建表。Windows 的独立 Worker 使用 Selector 事件循环，以兼容 psycopg。测试只在独立测试库初始化 `autumn_checkpoints_test_h`，未初始化业务库的框架表。

检查点只保存 Run ID、代际、步骤和路由；正文、身份快照、权限与工具参数不从框架状态恢复。重启会从业务数据库重读当前资格和上下文。框架表的备份/保留策略可后续配置，当前没有自动删除业务历史。

## 处理器与依赖

| 任务类型 | 行为 | 默认装配 |
| --- | --- | --- |
| run.dispatch / run.resume | Agent 图、当前授权、工具与正式回复 | 需要真实模型/知识/saver |
| action.execute | 已人工确认的发布、撤回、删除、AI 限额、明确记忆 | 是 |
| storage.finalize / storage.delete | 幂等对象操作后联合提交文件状态与任务终态 | 是 |
| knowledge.ingest | 当前版本解析、外部嵌入、索引切换与账本结算 | 需要 Embedding |
| knowledge.publication_sync | 当前公开投影同步、撤下旧公开索引、排队构建 | 需要知识服务 |
| knowledge.cleanup | 只撤下当前失效索引，保留正文与历史引用 | 是 |
| run.cancel | 尝试取消配置匹配的上游调用，保留未知费用 | 是 |
| conversation.cleanup | 已软删除本人会话的派生摘要失效 | 是 |
| auth.email | 验证邮箱 / 重置密码邮件，未知结果不重发 | 需要有效 SMTP 配置 |

实施建议中的 `knowledge.index` 对应现有契约 `knowledge.ingest`，未增加同义任务类型。`jobs.queue` 不认识处理器；允许 `workers → agent/services`，禁止反向依赖。

默认租期 60 秒、续租 15 秒、空队列轮询 1 秒，每次队列操作使用独立短事务。处理器和续租不共享 AsyncSession。主循环只检查处理器是否已联合结算，不自行补写成功。

## 租约、重试与恢复

续租失败取消处理器，并尽可能取消上游；等待停止有界。已经发出的请求可能无法撤销，但迟到结果不能取得提交资格。服务在结果事务末端复核 lease_token、数据库实际时间、Run generation、当前权限与来源。

旧代际任务只能结束自己，不会结束新的 Run。失败的已确认动作会原子标记 failed，相关当前 Run 同时失败；目标写入不会部分提交。验证过期不等于再次确认，动作需要重新预览与确认。

文件/索引任务因会话或额外验证过期可进入 waiting_auth。站长重新登录/验证后：

- `GET /api/owner/jobs` 返回本人待验证任务的 ID、版本和类型。
- `POST /api/owner/jobs/{id}/resume`，请求 `{ "expected_version": 任务版本 }`，验证本人归属、当前站长额外验证及无未结清供应商调用后换绑当前 Session。

Run 的恢复继续使用既有聊天恢复接口，不以文件任务接口换绑 Run 或已确认动作。尝试次数不会重置；次数用尽后需要人工检查。服务不会自行延长站长验证时效。

## ProviderCall 与受控对账

模型、索引嵌入、检索嵌入和可选重排均先持久派发记录，再事务外调用。外部幂等键是稳定 logical_call_key 的 SHA-256，不含正文，也不随物理 attempt 改变。供应商不支持幂等时适配器可以忽略键，但必须遵守 unknown 禁止自动重放。

run-only 检索以当前 Run 版本、代际和逻辑调用键仲裁；任务调用同时校验当前 lease。只有上一物理尝试确定失败后才能创建下一次 attempt。同 Run/Job 的 dispatched/unknown 会阻止新派发，未知费用保持 null，不视作零或触发自动退款。

对账入口只有站长当前额外验证可以使用，不作为 Agent 工具：

- `GET /api/owner/provider-calls/unknown`：最多 100 条元数据及当前仲裁版本。
- `POST /api/owner/provider-calls/{id}/reconcile`：站长根据供应商账单/回执人工确认终态。

请求字段包括 `expected_version`、`status`（succeeded/failed）、`external_request_id`、`evidence_sha256`（供应商证据摘要），以及可选 usage、actual_cost、currency。绑定 Run 的调用还必须提供查询返回的 `expected_run_version` 和 `expected_generation`。有费用必须有币种，未得到明确费用时可以继续保留 null。

服务校验当前身份和版本，CAS 结算账本，同时追加证据/结算字段摘要审计。相同请求重复提交只返回既有结果；换证据、费用或终态会冲突。它不保存供应商回执正文，不自动生成 assistant 消息、重新排队原调用、退还次数或跨代际恢复上下文。供应商主动查询适配器尚未接入，当前使用明确的人工对账入口。

## 后台收敛

Worker 在领取前、之后每约 60 秒运行一次有界收敛；单个任务处理较长时下一次收敛在该任务结束后运行，另一个 Worker 也可独立收敛。候选每类最多 100 项，每项新建事务并重锁复核：

1. 回收过期 lease：安全、已知可幂等的任务重新排队；派发中/未知调用转 unknown 并停止相关执行；旧 token 始终无效，尝试次数用尽不再排队。
2. 无有效执行者且派发超过一小时的孤立调用标记 unknown，包括 run-only 记录，保留人工对账。
3. 超过一小时、失败/取消 Run 的未派发 RESERVED 预留释放。活跃或待用户恢复的 Run 不按年龄释放；已模型派发的额度不按此路径退款。
4. pending_delete 的已知文件错误按有限次数退避重试，沿用原任务授权绑定，不伪造新 Session 或验证资格。验证失效时等待站长；未知外部结果与次数用尽时不自动重试。
5. 当前失效索引排队撤下，已删会话的摘要失效；业务正文、聊天、来源及审计继续永久保留。

没有自动清空表、重建业务库或对生产库执行迁移。

## 基础验证

H 的 14 项基础流程与 5 项受影响 Agent 流程集中核对，覆盖领取过滤、续租失效、崩溃回收、文件验证恢复、索引公开投影、确认动作、账本稳定键、取消、对账幂等、额度保留与待删文件重试。仅使用 5442 独立 PostgreSQL、确定性模型/嵌入端口和实际 LangGraph/Postgres saver；不请求付费供应商或邮件。

每个 H 节点本地提交，未推送或部署。H 阶段本身没有请求真实供应商；后续已完成真实模型/嵌入/搜索适配与有限基础验收，见 [PROVIDERS_ACCEPTANCE.md](PROVIDERS_ACCEPTANCE.md)。邮件适配器已补齐基础检查，但真实邮箱登录/收件、后续 CI、可观测性和横切验收仍待完成。
