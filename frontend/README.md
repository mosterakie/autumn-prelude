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

首页插画由内置 imagegen 生成，提示词和路径见 [素材记录](../docs/design/asset-prompts.md)。像素头像为项目内的 SVG，支持减少动画与收起。

## 接入限制

原前端实现阶段的 23 项测试与生产构建已通过，浏览器检查过桌面、手机及主要演示流程。本次入口修复通过 TypeScript 和 4 项接口传输基础测试，未重跑生产构建或完整浏览器联调。后端已实现认证、TOTP、任务、Agent 和真实 AI 供应商接入，联合验收范围见 [助手验收说明](../backend/ASSISTANT_CONTENT_ACCEPTANCE.md)；真实邮箱发送仍未配置。上传暂按文档固定为 20 MiB，待 /public/site 配置结构冻结后改为服务端配置。角色配色是默认主题，不提供未约定 API 的伪保存按钮。
