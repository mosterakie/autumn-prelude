# G 阶段 Agent Runtime

G1–G7 的最小运行链路已接入真实 LangGraph 与现有应用服务。业务状态保存在
PostgreSQL；框架 checkpoint 只保存调度元数据。运行入口为
`AgentRuntime.execute(job_id, lease_token)`，仅供已领取任务的服务端调用。

## 工作流与结果

`authorize → context → plan → tool/wait/reply`。每次检索后再次进入 authorize/context。
模型返回严格 JSON 的工具建议、补充请求或回复；普通用户只有站内资料检索，站长
当前升级验证有效时可生成固定操作预览。发布/撤回/删除、AI 限额与记忆修改均需
用户确认，不提供模型确认、执行、任意 SQL 或秘密读取工具。

规划调用通过 `commit_model_step` 结算供应商账本，Run 保持运行中，不保存工具
协议为 assistant 消息。正式回复经 E8 提交闸门后才写 message 和终态，同时结束
任务；SSE 读取累计消息快照。此阶段没有供应商逐 token 流式接口。

补充请求和操作预览分别进入 waiting_input / waiting_approval，并在同一事务完成
当前任务。确认或补充仍走 F/E 服务，产生新的 run.resume 或 action.execute 任务。
动作 handler 成功后，来源闭包清空并切换执行代际，Agent 重新装配当前上下文；
数据库中已成功操作的白名单结果可用于回复，不能当作再次执行授权。

## 身份、授权与恢复

- 从任务绑定的 User/Session 重建身份，检查本人 conversation/Run、固定模式、
  当前 step-up、任务归属/lease 和执行代际。图状态或工具参数不能替换身份。
- 服务端 thread_id 为 `conversation:{id}:mode:{mode}`；图状态和 checkpoint metadata
  记录 Run/代际。LangGraph 根图使用空 namespace，业务隔离由数据库归属和代际保证。
  应用标记使用 autumn_run_id / autumn_execution_generation，避免框架把同一业务 Run
  当成继续旧节点，跳过恢复入口。
  每次业务恢复从授权节点开始，不能通过旧 checkpoint
  的正文或节点位置跳过授权。
- 原问题与补充答案来自本 Run 的实际用户消息。历史最多选取 12 条完整消息，
  通过 capture_dependencies 验证、登记闭包后才进入模型。失效历史直接省略。
- 当前代际片段在恢复时从数据库重新读取。摘要和确认记忆已有 E6 依赖捕获入口，
  本阶段默认上下文仅自动选取最近历史与 Run 中已登记片段，不自动生成摘要或画像。
- 全部检索/上下文写入和等待提交均带 TaskFence；权限、Run 版本/代际、租约复核
  与正式结果写入在同一短事务内完成。网络、向量端口与对象 I/O 不持有 UoW。
- 登录或额外验证失效进入 waiting_auth。F6 仅允许同一账号的新有效 Session 换绑，
  不重新扣次；首次装配前过期时，只允许补齐当前 epoch 的空来源闭包。

## 预算与供应商账本

首次运行冻结 `Settings.agent_limits`，Run.config_snapshot.agent_runtime 持久累计：

| 项目 | 默认上限 |
| --- | ---: |
| 模型调用 | 4 次 |
| 工具调用 | 4 次 |
| 输入保守单位 | 120000 UTF-8 字节 |
| 总输出预占 | 16384 tokens |
| 每次模型输出 | 4096 tokens |
| 活跃运行耗时 | 120000 毫秒 |

等待时间不计入活跃耗时；恢复继承原 Run 的预算和额度桶。每次输出按最大值预占，
供应商未返回 usage 不按零处理；输入字节是保守预算单位，不声称等于供应商 token。
模型调用先准备 ProviderCall，再通过 E8 标记 dispatched 与首次扣次，随后才在
事务外调用 ModelDriver。多次规划、工具循环及恢复总共只扣该 Run 一次问答额度。

逻辑调用身份为 Run + 代际 + 累计模型调用序号，转换成不含私人参数的外部幂等键。
存在 dispatched/unknown 记录时不继续派发，留给 H 的受控对账。失败、超时或权限
变化后的不确定调用保留 unknown，不把费用写成零或直接退款。

## 资料与来源

停云方向系统提示、实际当前用户问题、工具 schema 与 untrusted_data 分开序列化。
历史、网页、PDF、Word 和检索片段均不构成执行授权；资料中的角色/系统指令只是正文。

每个进入模型的检索片段先登记 run_sources，保留 revision、publication、观察到的
ACL 版本、index/chunk 与 locator；历史依赖也复制到当前执行代际。
每轮准备模型调用时在 model_inputs 保存 call_id、代际、实际用户消息 ID、来源 ID
和 context_manifest，不保存额外正文。正式消息在发送和读取时继续检查当前完整闭包。

scope_epoch 变化或来源失权时，旧代际停止。运行中的模型请求每秒复核当前权限和
lease，支持时请求供应商取消；不保证已经发送的外部请求能够撤销。迟到输出不能
写正式结果。ACL 失效使用既有 source.invalidated 事件并转 failed /
ACL_CONTEXT_INVALIDATED，不新增 run.invalidated 事件或主状态。

checkpoint 只包含 Run ID、代际、步数和路由。节点异常转换为稳定错误码后再交给
框架，防止供应商异常中的正文进入 checkpoint；私人正文只由单次调用的服务端
上下文持有，不写入图状态、任务结果、事件或审计 metadata。

## 验收与后续范围

测试使用 5442 的独立 PostgreSQL 测试库、真实 UoW/Repository/Policy/LangGraph，
模型与向量端口使用确定性测试实现，没有请求真实 DeepSeek/Tavily/百炼或邮件服务。
基础验收覆盖工具身份、预算、检索来源、等待恢复、人工确认后 ACL 重建、生成中
权限撤回与 checkpoint 内容边界，具体结果见 DEVELOPMENT.md。

H 接入 Worker 主循环、registry、heartbeat、回收、业务 handlers 和受控对账；
Postgres checkpoint saver 的安装/初始化也在 H 接入，本阶段仅支持注入 saver。
I 接入真实供应商与邮件端口。联网搜索尚未注册为工具，不伪称已联网。完整工具清单、
自动摘要/记忆选择及保留策略管理继续按后续功能扩展。网站真实模型端到端运行尚待 H/I。
