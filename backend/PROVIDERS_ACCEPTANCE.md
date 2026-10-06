# 真实 AI 供应商接入与基础验收

验收日期：2026-10-06。结论：**真实 AI 后端链路的基础验收通过，完整 v1 站点尚未验收通过。**

## 当前配置

| 能力             | 供应商与模型              | 实际结果                             |
| ---------------- | ------------------------- | ------------------------------------ |
| 对话与 JSON 计划 | DeepSeek / deepseek-flash | 模型列表校验与真实规划、回复成功     |
| 知识库向量化     | 百炼 / text-embedding-v4  | 工作空间兼容地址实际返回 1024 维向量 |
| 站长联网搜索     | Tavily / basic            | 搜索、引用登记和模型回答成功         |

实际账号的模型列表包含 deepseek-flash 与 deepseek-v4-pro；本轮使用 Flash。模型名可以通过 `AUTUMN_LLM_MODEL` 调整。百炼地址由账号工作空间配置，不能用未经验证的公共地址替换。

凭据仅存于 Git 忽略的 `backend/.env.providers.local`；Settings 先读 `.env` 再读此文件，进程环境变量优先。仓库仅提交变量示例，日志和错误不返回供应商响应正文、Authorization 或密钥。

## 实际验证范围

1. 普通用户：公开内容建立独立向量索引 → 受理 Run → DeepSeek 规划一次站内检索 → 百炼生成查询向量 → 当前获准来源登记 → DeepSeek 回复 → 正式消息、任务终态和调用账本提交。
2. 站长：当前额外验证有效、owner 会话、显式联网运行 → DeepSeek 规划搜索 → Tavily 返回摘要 → 来源与成功账本联合提交 → DeepSeek 引用来源回答。
3. 10 项本地基础用例：供应商请求/响应协议、系统角色隔离、错误与配置脱敏、不重试、截断回复拒绝、普通用户联网拒绝、旧任务资格拒绝、验证过期后拒绝迟到来源、向量分批账本，以及受影响的知识与 Agent 流程。

两项真实链路分别通过复验，每条最多两次模型请求、每次最多 512 输出 token，站长链路最多一次 basic 搜索。未运行全量测试或扩展并发矩阵。数据库位于 5442 独立测试库，业务事务结束回滚；使用已初始化的 `autumn_checkpoints_test_h`，测试结束移除本用例的框架检查点。未写业务库或执行迁移。

包含首次失败和两项复验，本轮共发出 8 次模型、4 次嵌入、2 次 basic 搜索请求，以及 1 次只读模型列表请求；不估算或伪报实际账单金额。Ruff、127 源文件 mypy strict 与 Alembic 无漂移检查通过。

首次真实知识库链路未通过，模型和嵌入 HTTP 已成功，但运行未形成成功结果。补充当前执行的 `tool_results` 后复验通过：模型明确看到检索步骤已完成及来源 ID，随后生成回复。该信息来自实际成功工具结果，只保存在当前执行的内存上下文，不进入框架图状态，也不提供执行授权；等待恢复仍从业务数据库重读来源。不能把首次失败算成通过，也不能据此保证所有问题都只检索一次。

本机报告位于工作区 `outputs/backend-development/`：`pytest-providers-basic.xml`、`pytest-providers-live-knowledge.xml`、`pytest-providers-live-web.xml`。首次联调记录 `pytest-providers-live.xml` 保留失败结果，供追溯。

## 运行边界

- HTTP 事务外执行，每次请求有总超时和响应大小限制，禁止自动重试和重定向。提供商是否支持幂等没有得到保证；外部逻辑键用于本地账本，不能宣称能防止上游重复计费。
- 模型 system role 包含服务器约束、工具 schema 和输出 schema；历史与检索材料放在 user role 的独立资料区。模型不能传入身份、确认或执行动作。
- 联网仅在 owner 会话、当前站长额外验证、运行 `web_enabled=true` 且搜索模式 web/auto 时执行。模型即便伪造工具名，服务仍在请求前重新鉴权。返回后重验 Run 版本、代际、权限和 lease。
- Tavily 只保留有限摘要，不自动下载结果网页；来源 URL 校验后才登记。记录实际返回的 request_id 与搜索单位，不虚报金额。
- 百炼每次最多 10 个片段，每批有独立外部调用账本；最后一批结算与索引激活、任务完成共同提交。中间失败不启用部分索引。已知成功或未知批次都不能自动重放。
- 取消只尽力关闭本地请求，不能保证上游停止计费。超时、迟到结果和未确定费用沿用 unknown/受控对账，不按零费用处理。

## 启动与重现

安装 `dev,agent,providers` extras，配置数据库及示例中的供应商变量。Worker 默认工厂会装配真实端口；缺少必要配置不会提供伪造回复。启用模型还必须有嵌入服务和持久检查点。

框架表只通过显式 `python -m autumn_backend.workers --setup-checkpoints` 初始化，schema 使用 `AUTUMN_CHECKPOINT_SCHEMA`。普通 Worker 启动只检查表版本。此轮没有启动持续运行或部署服务，也没有初始化业务数据库的框架表。

真实验收测试默认跳过。使用独立测试库并显式设置 `AUTUMN_LIVE_PROVIDER_TESTS=1`，再选择 `tests/integration/test_live_providers.py` 中具体用例；这会产生真实供应商费用。本轮复验的运行预算为模型 2 次、工具 1 次、输出总预占 1024、每轮 512、总耗时 120 秒。

## 完整 v1 仍待完成

- 真实邮箱发送与注册验证/找回链路；后续已接入 163 SMTP 和基础检查，授权码填写后的真实登录/收件尚待验证，见 [邮箱配置](EMAIL_CONFIGURATION.md)。
- 浏览器到 API/Worker 的完整联调，包括知识库文件/网址 HTTP 入口与前端交互覆盖；本轮通过服务与图运行器验证，没有宣称浏览器已验收。
- 原实施顺序及修订建议 I 阶段的依赖检查、可观测性、横切验收、评审闸门和冻结复核。接入真实供应商不等于 I 阶段已完成；统一状态见 [项目归档](../docs/archive/README.md)。
- 部署继续暂缓。

协议核对依据：[DeepSeek Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)、[百炼兼容嵌入接口](https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai/)、[Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)。
