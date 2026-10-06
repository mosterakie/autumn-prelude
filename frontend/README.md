# 秋序独立前端

Next.js App Router、TypeScript、Tailwind CSS 与 TanStack Query。依据 [前端设计](../docs/architecture/frontend.md) 和 [接口契约](../docs/architecture/api-contract.md) 实现；FastAPI 业务与 Agent 位于独立的 backend 目录。

## 启动

Node.js 22.12+，建议 24 LTS。

```powershell
npm ci
npm run dev
```

打开 http://localhost:3000。本地环境禁止 Next CLI 创建子进程时，可使用 `npm run preview` 的同进程预览。此入口仅启动 HTTP 服务，不承载业务逻辑。

```powershell
npm run check
npm test
npm run build
npm start
```

依赖版本在 package-lock.json；格式化用 `npm run format`。

## 数据模式

默认 demo：登录、回复、入库都是易失的交互模拟，不发送邮件、不调用模型、不抓取网页、不持久保存。刷新、退出或热更新可能重置示例数据。

| 演示邮箱 | 体验 |
| --- | --- |
| visitor@example.com | 普通用户，无联网开关 |
| owner@example.com | 站长，额外验证码 123456 |
| cooldown@example.com | 验证后 24 小时冷却 |
| limited@example.com | 每日 10 次额度已耗尽 |
| unverified@example.com | 邮箱未验证与重发邮件入口 |

密码填任意 8 位示例字符，请勿输入真实密码。邮箱前缀切换身份只存在于演示适配器，绝不可用于真实认证。

真实 API：复制 .env.example 为 .env.local，修改并重启：

```dotenv
NEXT_PUBLIC_API_MODE=api
FASTAPI_ORIGIN=http://127.0.0.1:8000
```

SSR 从 FastAPI 获取公开 DTO；客户端访问同源 /api/*，由 Next 透明转发。连接失败显示错误，不回退成示例成功。前端不持有 DeepSeek、Tavily、百炼密钥，不签发身份，不直连数据库。

## 页面与结构

- 公开：首页、手记列表与详情、收藏、留言、关于、404。
- 账号：登录、注册、验证邮箱、找回密码、重设密码、账号与额度。
- AI：公开与私人会话、历史分页、重命名、删除、来源弹窗、停止、等待输入与验证恢复。
- 工作台：草稿、公开字段预览与确认、撤回、文件与网页处理队列、审核、额度与保留策略、手动记忆和审计入口。

公开页面免登录，提交留言时弹出登录框并保留输入。管理组件在额外验证有效时才挂载；到期、退出和身份切换清除敏感视图、缓存与订阅。

站长登录后，导航直接进入 `/admin/chat` 私人助手，需完成验证码额外验证。私人助手默认“自动判断 · 可联网”，可改为仅站内或允许联网；普通 `/chat` 仍为公开资料问答，站长进入此页时会看到切换提示。

站长可从手记页、收藏页、账号页及工作台使用“写手记”和“收藏网址”。`/admin/content?create=article` 与 `?create=bookmark` 直接打开对应表单；先保存为私人内容，公开版本另行预览确认。助手也可生成收藏和手记的待确认预览，由用户确认后保存。编辑和删除沿用后端要求的幂等请求键，网络结果未确定时重试不会换键。

```text
src/app/          页面与布局，公开内容优先 SSR
src/components/   阅读、账号、对话与工作台交互
src/lib/          DTO、API 传输、内存演示、安全规则
public/images/    独立首页角色插画
tests/            安全边界、接口传输、演示工作流测试
scripts/          可选同进程预览入口
```

首页插画和新版聊天像素形象由内置 imagegen 生成，提示词和路径见 [素材记录](../docs/design/asset-prompts.md)。聊天使用透明 PNG，桌面显示 96 × 112 px、手机 72 × 84 px，保留状态文字、减少动画与收起功能。

## 接入限制

2026-10-06 更新停云像素形象：从原先约 30 × 33 px 的内嵌 SVG 换为透明 PNG，桌面区域 96 × 112 px、手机 72 × 84 px。TypeScript 与独立浏览器组件检查通过，确认图片加载、状态提示、收起/恢复，以及手机宽度下无横向溢出或额度重叠；未重跑生产构建或业务链路测试。

2026-10-06 修复聊天等待输入漏接固定选项：有 options 时显示单选项并原样提交，无选项才显示文字输入；提交失败保留答案，过期时禁用提交并提供停止入口。恢复后更新完整 Run 状态并重连事件。保存和公开仍通过操作预览“确认执行”，补充答案不会执行写入。单选按钮已排除普通输入框的宽度样式。TypeScript 通过；独立浏览器页面使用正式表单组件，检查选择提交、无选项文字输入、计时刷新和过期/停止流程，未向当前用户任务提交答案或调用模型。后端基础结果见 [助手验收说明](../backend/ASSISTANT_CONTENT_ACCEPTANCE.md)。

2026-10-06 修复共用弹窗的输入失焦：会话计时器每秒更新，而内联 `onClose` 回调的变化原先会重复触发聚焦和滚动锁定。现仅在弹窗挂载/卸载时处理焦点，关闭事件读取最新回调；打开时优先聚焦可用输入框。已在本地浏览器复现旧问题，并验证邮箱/密码持续输入、跨计时刷新继续输入、Tab/Shift+Tab 循环，以及 Escape 关闭后的焦点和滚动恢复；TypeScript 检查通过。验证使用示例输入，未提交登录表单。

原前端实现阶段的 23 项测试与生产构建已通过，浏览器检查过桌面、手机及主要演示流程。本次入口修复通过 TypeScript 和 4 项接口传输基础测试，未重跑生产构建或完整浏览器联调。后端已实现认证、TOTP、任务、Agent 和真实 AI 供应商接入，联合验收范围见 [助手验收说明](../backend/ASSISTANT_CONTENT_ACCEPTANCE.md)；真实邮箱发送尚未接入 SMTP，配置状态见 [邮箱说明](../backend/EMAIL_CONFIGURATION.md)。上传暂按文档固定为 20 MiB，待 /public/site 配置结构冻结后改为服务端配置。角色配色是默认主题，不提供未约定 API 的伪保存按钮。
