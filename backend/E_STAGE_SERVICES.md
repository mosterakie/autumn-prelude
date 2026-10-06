# E 阶段服务契约

E1 发布、E2 问答受理、E3 留言提交分别见现有服务契约。这里记录 E4–E8 的服务入口、
事务边界和后续 F/G/H 接入责任。每个节点仅做必要的基础集成验收，本地提交，不推送。

## E4 额度结算

QuotaService.current 根据当前时间/角色配置装配今日额度，只读，不为查询创建新桶。
结算方法以 `_in_uow` 结尾，供可信执行器组合进同一个 UoW，不自行提交或执行外部调用。
执行器必须先完成当前权限、job lease 和 run generation 检查；实际模型网络请求发生在提交之后。

- dispatch_model_in_uow：校验调用属于 Run 且用途是 chat/tool；prepared→dispatched
  标记与 reserved→charged 同事务。每个 Run 只扣一次；重试不产生第二次扣次。
- release_before_dispatch_in_uow：只允许未派发模型调用的预留释放。调用后停止、结果不明
  不能走此路径；账本仍记录 unknown，不能当作零成本自动重发。
- refund_failure_in_uow：仅接受服务端可信故障分类；幂等 charged→refunded，账本不删除。
  这是内部编排入口，不是开放给 HTTP 或模型参数的退款工具。
- cleanup_candidates：仅返回老旧 reserved 且 Run 已失败/取消、无模型派发的候选 ID。
  F/H 调度需逐个重新锁定/复核后释放；本节点不启动清理，不按年龄释放活跃任务。

日桶一直使用 Reservation.bucket_id，不随午夜、配置修改或恢复改绑。
锁顺序为 user→conversation→run→provider_call→bucket→reservation；上层联合结果提交须遵循相同顺序。
基础验收：2 项真实 PostgreSQL 流程通过，覆盖派发扣次/停止/故障退款、调用前释放与晚期故障回滚。

## E5 文件生命周期

StorageService.upload 仅允许已升级站长，支持非空、最多 20 MiB 的 PDF / DOCX；
扩展名、内容签名、MIME、DOCX 展开大小一起校验。请求身份由账号与幂等键生成稳定 UUID，
对象 key 从 staging 到正式区保持不变。相同键不同内容冲突，ready 重试直接返回原记录。
校验及物理写入在短鉴权事务之外，随后重新鉴权，FileObject(staged) 与 finalize Job 同事务登记。

finalize / run_delete 是供 H 执行器调用的任务处理入口：两端均复核当前站长与有效 lease，
中间进行物理移动/删除；末端文件状态与 Job.finish 同一事务。物理操作可重复，租约丢失
不提交正式状态；物理成功而数据库失败时重试收敛。运行循环、退避和租约回收在 H 接入。
delete 先记录 pending_delete 并阻断读取；关联资源必须提供双版本，同时撤销投影及权限 epoch。
删除磁盘失败保留 pending_delete。read_private 两端检查身份、文件和关联资源，返回前校验摘要。

cleanup_orphans 接受明确时区时钟，仅移除超过 24 小时且没有数据库登记的 staging；
已登记文件由任务流程收敛，不按年龄删除。LocalObjectStore 限制规范对象 key、解析后路径
必须位于 storage_root 内；通过 ContextVar 阻止在活跃 UoW 内调用对象存储。
E8 复核补充永久小删除标记，并先删 staging 再删正式文件；迟到的上传/转正不能复用已删除 key。
基础验收 3 项：上传/转正/重试/权限、删除失败重试与立即阻断、孤儿回收保留登记对象。

## E6 知识库与来源

KnowledgeService.ingest_file / ingest_web 将提取结果落入不可变 resource_versions，并登记
knowledge.ingest 作业。文件版本保留原文件摘要、MIME、大小和对象 key；重复导入复用原资源。
PDF 用 pypdf 提取可选中文本，DOCX 用 ZIP/XML 读取段落，拒绝实体、扫描空文本和旧 DOC。
私人文件索引保留页码/段落；公开索引始终使用 publication 的实际白名单投影及字段内偏移，
不会为方便索引而读取原文件或完整私人正文。发布旧版本后编辑新原稿不会改写已发布投影。

SafeWebFetcher 仅接受公开 HTTP(S)，禁止 URL 凭据/本地 IP/非标准端口；逐跳解析并检查
全部地址，再固定连接到已验证的公网 IP（HTTPS 仍验证原主机证书），最多 3 次跳转、
2 MB 响应、无 Cookie/代理/登录。提取静态 HTML/纯文本，不运行脚本；正文没有可提取文字
时拒绝。DNS 使用系统解析器；网络超时和 worker 预算由后续运行器统一控制。

request_index / build_index 锁定当前站长、资源双版本、任务有效 lease 和嵌入模型身份；
提取/分块/嵌入在 UoW 外，返回后重新复核。新 generation 与 chunks 建完后，旧索引退役、
新索引激活、Job.finish 同事务。sync_publication 承接 E1 的通知，过时通知只标记 superseded；
有效通知退役旧公开索引，再排队当前投影或完成撤回。实际嵌入与 rerank 供应商适配留在 I。
端口强制 1024 维、有限非零向量，查询限定 provider/model/dimension，不混用模型。

retrieve 先在 SQL 中选择当前允许的 active/ready 索引，锁资源后重查，再对这一范围执行
精确向量排序。公开模式只查未撤回且 ai_enabled 的投影；站长私人模式只查本人当前原稿。
可选 rerank 在 UoW 外，返回 ID 必须来自候选；末端再检查当前范围、Run version 与 ACL。
返回给模型的全部片段登记 run_sources，绑定确切 revision/publication/ACL/index/chunk/locator。
record_web_sources 仅供获准站长运行登记实际联网输入；citation 复核 Run 归属与当前来源权限。

capture_dependencies 返回实际选入的历史消息、摘要、记忆文本，并复制/验证其所有来源依赖。
摘要追踪截至 upto_message_seq 的关联 runs，记忆追踪 origin_run / origin_message；缺失来源
manifest 或失效权限拒绝继续。服务生成 schema_version=1 的完整 manifest，不接收客户端的
“完整”声明。G 必须仅使用这些入口返回的文本，并在每次派发前再次验证完整闭包；E 的端口
不是完整 LangGraph 上下文运行器。Run.version 用作乐观版本检查，E8 的 execution_generation
独立用于执行 fencing；run_sources.context_generation 区分当前闭包与历史依赖。

基础验收 4 项：私人/公开隔离与撤回引用、DOCX/网页版本与无 OCR、嵌入期间撤回丢弃结果、
历史来源闭包复制后重新失效；外部端口测试同时断言 active_uows=0。

## E7 动作与补充信息

ActionService 接受严格、禁止额外字段的 ResourcePreview / SettingsPreview / MemoryPreview：
发布、撤回、删除资源，修改 AI 限额，新增/修改/删除本人记忆。schema_version=1 的 command
包含目标类型/身份、内容版本、ACL 版本、实际参数；规范 JSON 的 SHA256 与不可空
(actor_id, idempotency_key) 一起判定重试。没有资源 FK 的设置/新增记忆也按同一身份去重。
未知动作/参数拒绝；保留策略的具体预览参数与 handler 随 F 的保留设置入口扩展。

preview 总是 confirmed_preview + requires_confirmation=True；Agent 提议必须绑定本人
Run 与同会话用户消息，Run 进入 waiting_approval，事件只存 Action ID。模型不能提交身份、
授权方式或关闭确认。read 返回账号当前可见的预览。confirm 要求匹配展示的参数摘要、
Action version、目标双版本、当前站长升级验证；有 Run 时还复核完整上下文及账号执行名额。
Action ready、Run queued、唯一 action.execute Job 和脱敏审计同事务，确认不代表已执行成功。
实际各动作 handler 与 succeeded / 业务结果的原子提交由 H 调用 E8 联合校验入口完成。
F 的确认路由仅接收用户页面操作，不注册为 Agent 工具；E1 明确请求入口也不能给模型直接调用。

同键异语义冲突，ready 同体重试不重复入队。到期确认持久标记 expired；查看仍显示真实状态。
取消/过期同时终止关联的等待 Run，未派发模型则释放预留；派发后保留原扣次，不按取消退款。
确认已过期或被取消的动作不能在后台继续执行，H 必须检查当前 Action 状态及有效期。

InputWaitService.request 保存 schema_version=1 的 id/prompt/options/expires_at，Run 进入
waiting_input。answer 锁当前账号/会话/Run，重新鉴权及验证完整来源，检查有效期/选项/执行名额，
将 consumed_at、answer_hash、answer_message_id、用户消息、Run queued、run.resume Job 与事件
同事务保存；答案正文只在消息表。相同等待项与同答案返回原记录，异答案 409；没有新 Reservation、
新扣次、ask 速率或冷却判断。运行器在后续 G/H 负责等待超时扫描和状态机调度。

基础验收 4 项：预览/确认/幂等/归属及无资源目标、旧 ACL 与持久过期、答案去重/不重新收费、
入队后的故障回滚消费标记/消息/任务。服务只执行数据库操作，没有外部 I/O。

## E8 外部 I/O 与结果提交

UnitOfWork 在进入/退出时维护任务局部 active_uows。LocalObjectStore 和网页读取器
在 I/O 入口检查；KnowledgeService 的提取/嵌入/rerank、ExecutionService 的模型端口
在调用前检查。子任务继承事务标记，也不能借新 Task 绕过边界。E1/E2/E3/E4/E7
只有数据库操作；邮件发送尚未实现，F/I 接入邮件端口时必须使用相同边界约束。

新增持久 runs.execution_generation、run_sources.context_generation（默认 1，均 >=1）。
派发/恢复 Job 载荷绑定运行代际，manifest 绑定当前代际。等待/恢复在 Run 锁内推进代际
并复制已验证来源；权限修改后的重建推进代际并标记 manifest 不完整，不删除旧来源。
历史、摘要和记忆目前保守复核来源 Run 的全部代际依赖；有失效依赖时拒绝使用，G 负责
选择可用历史/重建摘要，不能为了继续而忽略依赖。切换代际本身不能宣称旧文本已获新授权。

ExecutionService.start_model_call 在同一短 UoW 内检查当前认证/本人会话/完整来源、
Job token/到期时间、任务及 Run 的代际、授权会话、ProviderCall 归属与 prepared 状态；
Run running、账本 dispatched 与首次额度扣次一起提交，再把不可变 ExecutionFence 带出事务。
已派发或 unknown 调用拒绝重复网络请求。call_model 在 UoW 外调用 ModelCall；
commit_model_result 再开 UoW，联合验证 lease、代际、Run version、当前身份及完整来源，
然后正式 assistant 消息、ProviderCall succeeded、Run succeeded、元数据事件及 Job.finish
同事务保存，并在最终 Job.finish 前复核实际时间。失效或晚期故障回滚全部正式结果。
没有将正文复制进事件/Job，也没有在失权时保存模型输出。外部调用后失权并不等于零费用；
账本保留派发证据，H 负责 unknown 对账/故障分类/退款，不能自动重发。

commit_action_result 只接受 H 的可信数据库 handler，不能作为 HTTP 或模型可调用接口。
联合复核 confirmed_preview 的参数摘要、双版本、状态、有效期、当前权限、任务 lease，
有关联 Run 时同时检查代际及旧输入闭包。handler 的业务写入、Action succeeded、审计与
Job.finish 在同一事务。结果仅允许对象 ID/版本/状态元数据；handler 必须执行已验证 command。
handler 可能有意修改 ACL/删除资源，因此末端检查当前身份/会话/归属/代际，随后使关联
Run 的上下文不完整、推进代际、排队 run.resume(rebuild_context=True)，不能续用旧输入。
发布、设置、记忆、删除等实际 handler 注册与执行循环仍由 H 接入。

正式结果写入的统一锁顺序：user/auth_session → conversation → run → 排序后的 resource 集合
→ job → provider_call/额度；动作在进入目标锁之前一起锁定其来源资源。Service 不持数据库
锁跨越模型/嵌入/网络/文件调用。I 的实际供应商适配必须遵守端口的 I/O 契约。

基础验收 5 项：模型端口在事务外及成功原子提交、旧 lease/代际丢弃、模型期间撤回来源、
动作业务写入后租约失效整体回滚及成功提交、对象 I/O 闸门覆盖子任务。

## 本阶段完成边界

2026-10-06 集中基础验收 31 通过、0 跳过（29 个服务流程 + 2 个 CHECK Catalog 核对）。
Ruff、格式、78 文件 mypy strict、git diff --check、增量迁移回退/升级与 Alembic check 通过。
测试库独立使用 5442；业务库未修改。各节点本地提交，没有推送或部署。

F 已接入邮箱/密码认证、站长升级验证、资源/动作/Run API 与 SSE；G 已接入
最小 LangGraph 与业务运行链路，见 [G_STAGE_AGENT.md](G_STAGE_AGENT.md)。H/I 继续
接入 Worker 调度/heartbeat/业务 handler 和 DeepSeek/Tavily/百炼/邮件。
E 的完成范围是应用服务和事务边界，当前网站尚未完成真实模型端到端联通。
