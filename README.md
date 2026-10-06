# 秋序 Autumn Prelude

个人网站与 AI 助手。对外提供个人手记、收藏、留言和普通用户 AI 问答；对内通过助手管理私人知识库、内容和公开范围。

已完成产品方案、架构与数据库设计，并实现第一版独立前端：公开阅读、邮箱账号流程、AI 对话界面与站长工作台。视觉采用停云方向的圆角暖白、赭红和柔金配色。后端已完成 A–F：数据库、数据访问、权限、应用服务和认证/资源/聊天/SSE/留言/额度/动作 API，进度见 [后端实施记录](backend/DEVELOPMENT.md)，接入范围见 [F 阶段接口说明](backend/F_STAGE_API.md)。前端可运行明确标注的内存演示及 FastAPI 接入模式；真实邮件发送、Agent、模型供应商与后台运行器将在后续阶段接入，部署暂缓。

## 已确定的方向

- 中文名：秋序。英文名：Autumn Prelude。
- 邮箱注册并验证，邮箱加密码登录。
- 公开页面免登录；留言、AI 对话、私人数据和管理操作需要鉴权。
- 普通用户邮箱验证后冷却 24 小时，之后每账号每日 10 次 AI 请求，参数可配置。
- 联网搜索仅站长使用；公开资料检索按当前权限过滤。
- 知识库优先支持公开网页链接、文本型 PDF 和 DOCX；首版不做扫描件 OCR 或旧版 DOC。
- 前后端分离，FastAPI 业务与 Agent 放在同一后端项目中。
- DeepSeek、Tavily 与百炼作为服务选型；具体分工与模型参数见产品方案。
- 文章、收藏、聊天和日志默认永久保存，保留期限调整和删除接口。
- 站长可通过 AI 调整内容与公开范围，表单和 Agent 共用业务服务。

## 目录

```text
frontend/       可运行的 Next.js + TypeScript + Tailwind CSS 前端
backend/        FastAPI 与 Agent 的统一后端目录
docs/           产品方案与已确认决定
docs/architecture/ 前后端、接口与数据库设计
docs/design/    原有视觉概念稿及说明
```

## 文档

- [完整产品方案](docs/product-plan.md)
- [已确认决定与待定项](docs/decisions.md)
- [技术设计总览](docs/architecture/README.md)
- [前端设计](docs/architecture/frontend.md)
- [后端与 Agent 设计](docs/architecture/backend.md)
- [接口契约](docs/architecture/api-contract.md)
- [数据库设计](docs/architecture/database.md)
- [设计概念稿](docs/design/README.md)
- [前端范围](frontend/README.md)
- [后端与 Agent 范围](backend/README.md)

## 开发原则

先验证“网页链接收藏入库 → 检索整理 → 站长指定公开字段 → 访客浏览与注册用户问答”的完整流程，再扩大功能范围。

权限、冷却与额度由后端执行。DeepSeek、Tavily 和百炼的真实密钥、用户上传文件、聊天记录及运行日志不提交仓库。

## 本地体验

使用 Node.js 22.12+，建议 Node.js 24 LTS：

```powershell
cd frontend
npm ci
npm run dev
```

打开 http://localhost:3000。默认是内存演示：登录 `visitor@example.com` 或 `owner@example.com`，填任意 8 位示例密码；站长额外验证使用 `123456`。请勿在演示里输入真实密码，预览不会发送邮件或调用 AI。刷新、退出或开发热更新可能重置示例数据。

页面范围、运行方法与后端接入见 [前端说明](frontend/README.md)；当前验证与契约补充见 [前端实现记录](docs/architecture/frontend-implementation.md)。
