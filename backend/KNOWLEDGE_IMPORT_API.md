# 知识导入接口与本地调试（2026-10-07）

本节点补齐真实 HTTP 导入和工作台需要的任务接口。没有新增表或迁移；沿用 FileObject、Resource、ResourceVersion、Job、KnowledgeIndex 与 ProviderCall。2026-10-06 的研究归档和 ZIP 是历史快照，接口现状以本文和代码为准。

## 启动前

在实际启动后端的 Python 环境里更新依赖：

```powershell
cd backend
python -m pip install -e ".[dev,agent,providers]"
```

本次新增 `python-multipart`，用于文件上传；API 的供应商装配也需要核心依赖 `httpx`。本地隔离审计环境已经安装上传组件。项目启动使用哪个 Python，就在那个环境安装依赖；不要仅更新另一个全局 Python。

数据库继续使用本机 PostgreSQL 5442。API 与 Worker 应从同一个 backend 目录启动，读取同一套配置与文件存储根目录。需要重新启动两个后端进程；前端保持 `NEXT_PUBLIC_API_MODE=api` 和正确的 `FASTAPI_ORIGIN`。

```powershell
python -m uvicorn autumn_backend.app:create_app --factory --host 127.0.0.1 --port 8000
# 另一个终端，在同一环境、同一 backend 目录
python -m autumn_backend.workers
```

文件和正文入库需要百炼嵌入配置；只收藏不需要嵌入、网页抓取或 Tavily。API 的 202 仅表示资源已持久受理；未启动 Worker 时任务保持排队。Worker 会处理真实任务，不要用真实业务队列做无副作用测试。

## 已注册接口

所有接口都需要登录的站长、已验证邮箱和有效的站长二次验证。写请求继续检查可信 Origin 与 X-CSRF-Token。导入和网页刷新必须带 Idempotency-Key；不接受客户端注入身份或权限。

| 方法与路径 | 请求 | 返回与边界 |
| --- | --- | --- |
| POST `/api/knowledge/files` | multipart：`file`，`title` 可选 | 202；文本 PDF / DOCX，最多 20 MiB；标题为空时用文件名 |
| POST `/api/knowledge/urls` | JSON：`url`、`mode`、`title` 可空、`tags` 可省略 | 202；仅公开 HTTP(S) 网页 |
| POST `/api/resources/{id}/refresh` | JSON：`expected_version` | 202；仅网页资料，重新抓取并创建私人新版本 |
| GET `/api/jobs/{id}` | 无请求体 | 当前任务状态、阶段、受控结果和固定错误提示 |
| POST `/api/jobs/{id}/cancel` | 无必需请求体 | 只取消 queued 任务；已开始处理返回 409；保留私人资源 |
| POST `/api/jobs/{id}/retry` | JSON：`expected_version` | 202；仅恢复 waiting_auth，需先完成登录/站长验证 |

`/jobs` 的三个入口只暴露本人知识导入/收藏任务，不开放聊天、发信或维护任务的通用控制。原 `/api/owner/jobs/{id}/resume` 继续可用。failed 任务不开放盲目重放；供应商结果未确定时，必须先走已有人工对账流程。网页资料可在问题处理后明确提交刷新；新刷新会产生新的嵌入调用。

知识库页面的处理队列只展示这些导入/收藏任务，其他后台任务继续由原有 owner jobs 等入口管理。网页刷新会保留原有标签和私人备注。

URL 请求示例：

```json
{
  "url": "https://example.com/article",
  "mode": "bookmark_and_knowledge",
  "title": "我的资料标题",
  "tags": ["研究"]
}
```

| mode | 保存结果 | 外部调用 |
| --- | --- | --- |
| `bookmark_only` | 一条私人 bookmark；Worker 将收藏任务结算为完成 | 不抓网页，不调用嵌入服务 |
| `knowledge_only` | 一条私人 webpage，抓取正文后建立索引 | 安全抓取 + 嵌入 |
| `bookmark_and_knowledge` | 一条私人 bookmark 和一条私人 webpage；bookmark 的 linked_source_id 指向资料资源 | 安全抓取 + 嵌入 |

没有自定义标题时，收藏先用网址作为标题，网页资料在抓取完成后使用页面标题。标签经过长度、数量和空值校验并规范化。收藏和资料具有独立的公开状态，关联不传播权限。

响应保持前端已有格式：`{data: {resource: ResourceDTO, job: JobDTO, bookmark: ResourceDTO | null}, request_id}`。JobDTO 新增 `version`、`can_cancel`；不可确定的 `progress` 为 null。不会返回任务 payload、租约令牌、对象存储路径或供应商异常原文。

## 实际处理链路

文件受理时校验后缀、内容签名、MIME、DOCX 压缩包大小及幂等身份，在暂存区保存原件，再创建私人资源、首个 revision 和索引任务。该流程不另外排一个并发的 storage.finalize：同一导入任务负责转正原件、提取文字、追加解析版本和建立索引。

网页受理时只创建资源和任务，不在 HTTP 请求里抓取。Worker 使用现有安全抓取器，逐跳检查公开地址、DNS 与重定向，不带 Cookie，禁止访问本地和内网地址。抓取或解析失败不会显示为索引成功；扫描 PDF、旧 DOC、加密或不可提取的文档不提供 OCR 回退。

对象存储、解析、抓取与嵌入均在 UoW 外。每次正式写入前后重读当前认证、资源内容/权限版本和数据库时间下的 lease token。原件转正、解析版本、准备标记和最终索引分别持久化；崩溃恢复复用现有阶段，已派发的嵌入仍受 ProviderCall 对账规则约束。已有私人及公开投影索引任务继续使用原索引服务，公开索引可绑定确切的旧公开版本。

同一站长、同一接口和幂等键的重复提交复用原资源及 Job；不同标题、网址、模式、标签或文件内容使用相同键会返回 409。文件上传总请求体另有 20 MiB + 64 KiB 的上限，包含无 Content-Length 的流式请求；文件自身仍不得超过 20 MiB。

## 你可以调试的步骤

1. 重启 API 和 Worker，登录站长并完成二次验证，打开 `/admin/knowledge`。
2. 上传有文字的 PDF 或 DOCX，观察排队、解析、建立索引、完成状态，完成后资料架会更新。
3. 分别尝试三种网页模式：只收藏应没有抓取或嵌入调用；其余两种应保存正文并生成索引。
4. 用私人助手询问已入库正文；需要使用站长私人会话和相应资料范围。公开聊天不能读取新导入的私人内容。
5. 对网页点击“重新处理”，确认生成新版本；文件和纯收藏不显示可用的刷新操作。
6. 若任务 waiting_auth，重新验证后点击“验证后恢复”；若 failed，查看固定错误提示和 Worker 日志。

所有新资源默认私人，不自动发布。真实百炼调用、真实网页兼容性及浏览器完整交互留给本地调试，本次没有调用真实外部服务。

## 基础验证记录

- 8 项 PostgreSQL 基础检查通过：网页三种模式及刷新、DOCX HTTP 上传/解析/索引/原件读取、权限/CSRF/输入/上传限制/取消、验证恢复与旧抓取防覆盖、原解析无 OCR 边界、旧公开投影 Worker 索引与清理。
- 使用 5442 的独立测试库，外层事务回滚，外部抓取和嵌入端口受控；未接触业务库或真实业务任务。
- Ruff、后端 mypy strict、前端 TypeScript、定点格式检查和 diff 检查通过。
- 没有新迁移、真实供应商调用、发信、全量回归、生产构建、推送或部署。
