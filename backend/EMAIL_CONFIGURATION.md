# 邮箱配置与本地使用

更新日期：2026-10-06。注册验证、重发验证和密码重置已接入 `auth.email` Worker 与 SMTP 适配器。本地使用 `x178111@163.com`，授权码由站长自行填写；当前授权码留空，尚未验证真实 SMTP 登录与收件。

## 填写位置

打开 `backend/.env.mail.local`，只需在以下字段的双引号内填写 163 邮箱的 SMTP 授权码：

```dotenv
AUTUMN_SMTP_PASSWORD="你的邮箱授权码"
```

这里使用邮箱服务的授权码，和秋序网站账号密码是不同的凭据。该文件已被 Git 忽略，授权码不要放到前端配置或提交到仓库。首次启用时在邮箱设置中开启 SMTP 服务并获取授权码。本网站只发送验证/重置邮件，不读取邮箱，因此无需配置 POP3 或 IMAP。

本地文件已预填：

```dotenv
AUTUMN_SMTP_HOST=smtp.163.com
AUTUMN_SMTP_PORT=465
AUTUMN_SMTP_USERNAME=x178111@163.com
AUTUMN_SMTP_PASSWORD=""
AUTUMN_SMTP_FROM_EMAIL=x178111@163.com
AUTUMN_SMTP_FROM_NAME=秋序
AUTUMN_SMTP_TIMEOUT_SECONDS=20
AUTUMN_FRONTEND_BASE_URL=http://localhost:3000
```

适配器使用 465 端口的隐式 TLS，校验证书与主机名，最低 TLS 1.2；不提供明文发信模式。协议依据：[RFC 8314 §3.3](https://datatracker.ietf.org/doc/html/rfc8314#section-3.3)、[Python SMTP_SSL](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP_SSL)。当前服务器配置来自站长提供的信息，真实连接仍需本地验证。

## 读取顺序与启动

设置由 [config.py](src/autumn_backend/config.py) 统一读取。`.env` → `.env.providers.local` → `.env.mail.local` 依次加载，后者覆盖前者同名值；进程环境变量覆盖文件值。通用模板为 [.env.example](.env.example)。现有模型配置仍保存在 `.env.providers.local`。

填写后重新启动 API 与 Worker，并保持前端运行。使用已安装后端依赖的 Python，在 `backend` 目录分别启动：

```powershell
python -m uvicorn autumn_backend.app:create_app --factory --host 127.0.0.1 --port 8000
python -m autumn_backend.workers
```

API 负责受理并入队，Worker 负责发信；只启动 API 不会发送邮件。授权码为空时 Worker 不注册邮件处理器，任务保持排队；维护会清理过期、已使用和终态任务的令牌密文。填入授权码后，仍有效的排队邮件可能被发送；旧链接过期后从页面重新申请。

API 和 Worker 必须使用同一个 `AUTUMN_AUTH_ENCRYPTION_KEY`，否则 Worker 无法解密排队凭据。此密钥不要为启用邮件而更改。

`AUTUMN_FRONTEND_BASE_URL` 是收件人打开的网站前端地址。当前 `http://localhost:3000` 仅适用于在运行网站的这台电脑上打开邮件；手机或其他电脑打开时需填写可访问的前端地址。日后部署改为正式 HTTPS 域名；它与后端 API 地址不同。

## 页面流程

1. 注册后收到 `/verify-email#token=…` 链接，打开页面并点击验证。令牌有效 24 小时、只能使用一次；成功后普通用户进入站点设置规定的 AI 冷却期，默认 24 小时、每日 10 次。
2. 登录页“忘记密码”提交邮箱后收到 `/reset-password#token=…` 链接，打开页面填写新密码并提交。令牌有效 1 小时；成功后旧登录会话撤销，需要重新登录。真实账号密码长度为 12–256 个字符。
3. 验证链接过期时从 `/resend-verification` 重新申请。注册、重发和找回密码统一返回受理结果，避免暴露账号是否存在；HTTP 202 不代表邮件已经送达。

邮件链接的令牌放在片段 `#token=…` 中，前端读取后从地址栏清除，不随页面请求进入服务器 URL 日志；保留旧 `?token=…` 链接兼容。打开页面不会自动消费令牌，需用户提交。邮件正文只包含固定说明、链接和期限，不含密码或私人资料。

站长额外验证继续使用 TOTP 或恢复码，与发信邮箱独立；邮箱配置不会把该邮箱的普通账号自动提升为站长。站长引导见 [AUTH_CONTRACT.md](AUTH_CONTRACT.md)。

## 失败时如何处理

| 状态/错误 | 含义与处理 |
| --- | --- |
| `SMTP_AUTH_FAILED` | SMTP 认证明确失败，核对服务是否开启、用户名和授权码，重启后从页面重新申请 |
| `SMTP_CONNECTION_FAILED` | 提交邮件前连接失败，检查网络、主机和端口后重新申请 |
| `SMTP_REJECTED` / `SMTP_RECIPIENT_REJECTED` | 服务端明确拒绝，核对发件与收件地址后重新申请 |
| `SMTP_OUTCOME_UNKNOWN` / `MAIL_REPLAY_BLOCKED` | 可能已经提交，先检查收件箱和垃圾邮件；同一任务不会自动重发，可主动申请新链接 |
| `MAIL_TOKEN_INACTIVE` | 凭据已过期、已使用或账号不再允许该操作，任务取消，不发信 |
| `MAIL_TOKEN_INVALID` | 凭据无法解密或哈希不匹配，检查 API/Worker 的加密密钥一致性后重新申请 |

错误与任务记录不会保存 SMTP 原始响应、授权码或链接令牌。成功表示 SMTP 服务已接受邮件，不保证最终收件箱投递；失败任务不自动重试，主动重新申请会生成新的令牌和任务。SMTP 本身不提供应用级的恰好一次投递，断连或进程中断按结果未知处理。

## 本次验证范围

8 项后端基础检查通过：TLS 配置与接受结果、认证拒绝、提交断连；真实 PostgreSQL 中的验证/重置链接、一次性消费与密文清理、处理器配置、过期取消与维护清理、派发重入阻止和结果未知结算。数据库用例按本用例 ID 操作并回滚，没有领取用户的既有邮件任务。2 项前端链接解析检查、前端 TypeScript、Ruff 和 129 源文件 mypy strict 通过。

SMTP 测试使用传输替身，没有发送真实邮件，也没有调用付费模型。授权码填写后的真实登录、邮箱投递与点击验证仍待站长本地核验；本节点无数据库迁移、全量回归、推送或部署。
