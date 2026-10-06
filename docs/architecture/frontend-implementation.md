# 秋序前端实现记录

日期：2026-10-05。阶段：第一版前端与本地交互预览；未部署。

## 当前结果

已初始化 frontend 为 Next.js App Router 独立应用，保留后端与 Agent 在 backend 的传统分离方式。公开路由服务端读取公开 DTO，交互通过统一 API 客户端调用 FastAPI。没有使用 Server Actions 实现业务，没有在浏览器存放模型密钥。

实现了设计文档的公开、账号、聊天与站长路由。首页沿用最近的停云圆角方案：暖白背景、赭红操作、柔金线条、大字标题与独立立绘。文章封面为可维护的 CSS 几何图形；聊天头像于 2026-10-06 换为新生成的透明像素风 PNG，桌面展示 96 × 112 px、手机 72 × 84 px。

演示适配器与正式请求分离。演示只用当前页的内存，不发送邮件、抓取网页、解析文件或调用模型。真实模式失败时显示错误，不报告模拟成功。清晰的预览标记保留在页面和操作结果中。

## 已落实的交互边界

- 当前身份来自 /auth/me；正式写入携带 Cookie 与内存中的 CSRF，客户端不提交 role 或 owner_id。
- 登录返回地址只接受安全站内路径；匿名留言触发弹窗，输入仍留在原组件。
- 私人和公开会话查询键包含用户与模式；退出通过 BroadcastChannel 同步，关闭流并清缓存。额外验证到期会卸载工作台，并移除敏感查询。
- 额度与冷却提示使用服务端时间偏移计算；页面提示不替代后端判定。
- 问答保留请求键、消息 ID 与原请求体；重试复用原请求。SSE 使用 run_id 与 after 游标恢复，不重新提交 ask。
- 累计快照按 content_version 整体替换；旧快照不能恢复已隐藏的失权消息。打开引用前正式模式重新查询 /citations/{id}。
- source.invalidated 后隐藏并刷新；scope.changed 关闭敏感展示并重新读取身份。waiting_input 和 waiting_auth 提供恢复原任务的入口。
- 草稿保存、公开预览、确认执行与撤回是独立动作。预览默认不选择私人备注和原文件下载；不把私人字段混进公共 DTO。
- 保存失败后的同内容重试沿用请求键，修改输入则生成新键；文件请求的身份包含内容摘要。
- 作业进度未知时保留 null，不编造百分比。202 只表示已受理，完成状态以 job 或 action 为准。
- Markdown 不执行 HTML，图片不自动拉取第三方资源，外链只接受 HTTP/HTTPS。

## 契约补充

刷新恢复需要明确的会话元数据入口。接口契约补充 ConversationDTO 与 GET /conversations/{id}，供当前模式与非终态运行恢复使用；这些仍需后端实现。

结合上一轮后端评审，公开预览、撤回与对应 ActionDTO 增加 expected_acl_version，与内容 expected_version 一同检查。前端不能完成数据库锁或事务控制，两个版本最终都由后端裁决。

RunDTO 正式字段为 id/current_message，SSE 快照为 message_id。前端适配到内部 run_id/message/id，避免把事件与完整 MessageDTO 混用。资源 PATCH 提交 expected_version 加扁平变更字段；execute 仅提交既有动作的 parameters_hash。

## 验证

已执行 TypeScript 检查、Next.js 生产构建及 23 项 Vitest 测试。测试覆盖外跳防护、上传限制、SSE 快照去重与失权屏蔽、Cookie/CSRF/请求键传输、错误不降级、multipart 请求、演示草稿与发布隔离、过期版本冲突及 ask 去重。

浏览器已检查 1280 × 900 桌面与 390 × 844 手机布局；已验证登录、公开问答及引用弹窗、额外验证、公开字段预览与执行、链接处理队列。手机使用单列阅读与可收起会话栏。

这些验证不能替代真实后端的权限、事务与并发测试。后端接入后需联合验证跨账号对象访问、真实邮件与 TOTP、冷却和次数结算、SSE 断线续接、权限撤回、文件解析错误、幂等冲突和作业恢复。

## 后续对接

1. FastAPI 按契约输出 OpenAPI，生成或校验 TypeScript DTO；不直接复制演示适配器为后端。
2. 明确 /public/site 的主题、外部账号、上传上限字段后，将当前固定配置改为服务端读取。
3. 文件解析由后端返回 OCR_REQUIRED 或 EXTRACTION_FAILED；前端已保留显示失败原因的界面，需联合验收。
4. 填入站长真实公开手记和收藏。演示内容不会自动导入数据库。
5. 部署继续暂缓。

技术依据：[Next.js 安装](https://nextjs.org/docs/app/getting-started/installation)、[Tailwind CSS 与 Next.js](https://tailwindcss.com/docs/installation/framework-guides/nextjs)、[Next.js 可选自定义服务入口](https://nextjs.org/docs/app/guides/custom-server)。
