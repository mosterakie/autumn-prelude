# 邮箱密码与站长验证

F2 提供真实 PostgreSQL 认证模块，F3 接入 HTTP。公开注册只能创建 member，页面和知识库操作仍各自检查当前权限。

## 密码与会话

- 邮箱去首尾空白并转小写，以数据库唯一约束仲裁；不合并邮箱点号或加号别名。首版接受常规 ASCII 邮箱，密码 12–256 字符。
- Argon2id PHC 编码串，随机 16 字节盐、64 MiB、3 次、1 lane；库实现散列与验证。密码计算在线程中且不持有 UoW。
- 浏览器保存 256 bit 随机会话令牌，数据库只保存 SHA-256 哈希。每次入口和敏感业务重新检查账号、auth_version、撤销、绝对/空闲期限。首版 GET 不延长空闲期限，默认 24 小时后重新登录，绝对期限 720 小时。
- 登录轮换同一账号当前 Cookie 会话，其它设备的有效会话保留。step-up 轮换当前会话；旧 Cookie 立即失效，新 session_id/递增 csrf_version 生成新 CSRF 签名。密码重置撤销该账号全部会话并递增 auth_version，要求重新登录。
- 所有已认证写请求必须同时满足可信 Origin 和 X-CSRF-Token；CSRF 使用独立密钥 HMAC 绑定 session_id + csrf_version。匿名注册、登录与链接操作也检查 Origin，防止登录 CSRF。
- 生产要求三份显式、至少 32 字符且非开发占位值的安全密钥，显式 HTTPS Origin，以及 Secure/HttpOnly/SameSite=Lax/Path=/、无 Domain 的 __Host- Cookie。开发使用独立 Cookie 名。认证响应 no-store。

## 邮箱链接与首次冷却

注册、重发验证、找回密码对已存在/未知邮箱使用统一受理响应。令牌具备 256 bit 随机性，数据库只存哈希。邮箱验证有效 24 小时，密码重置有效 1 小时。消费时锁账号和令牌并检查数据库时间，只能成功一次。

首次验证按当时 AI 设置写入固定 cooldown_until，默认 24 小时；后来再次验证不会延长或清除它。账号/IP 两套 HMAC 速率桶对成功和失败尝试均生效，计数使用独立短事务，错误不会回滚已消耗的尝试次数。不信任客户端 X-Forwarded-For；反向代理支持另行配置。

邮件与一次性令牌同事务创建 auth.email job。Job 只有对象 ID、用途、密钥版本与 Fernet 密文，无明文邮箱/令牌。消费后清除任务密文并取消尚未发送的 queued job。H 阶段实现 SMTP handler、发送结束和过期时的密文清理；本节点未发送真实邮件，尚未完成邮件注册体验联调。

## 站长引导

本地运行 `python -m autumn_backend.cli bootstrap-owner`，交互输入邮箱/密码，导入验证器并提交当前代码。验证成功后才创建 owner 与因子；全局事务锁保证首位站长引导互斥，既有邮箱不被提升为站长。脚本不会作为开发验收自动运行，也没有对应 HTTP/Agent 路由。

TOTP 为 RFC 6238 SHA-1、6 位、30 秒，接受当前时间步与前后一步，持久记录 last_used_time_step，已使用代码不可重放。随机恢复码只存哈希，8 个码一次性消费。TOTP 密钥通过独立环境主密钥认证加密；版本为 1，主密钥轮换需要受控密文迁移。引导 URI 和恢复码只在引导终端显示，不进入数据库明文或日志。

站长密码登录仍需 step-up 才能使用私人知识库或联网；角色不赋予他人聊天读取权。等待中的 Run 要在恢复路径受控关联轮换后的会话，不能用 checkpoint 替代身份验证。

原语依据：[Cryptography Argon2id](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#argon2id)、[RFC 6238](https://www.rfc-editor.org/rfc/rfc6238)。使用本机已有 Cryptography 50.0.1 验证，无需下载另一个密码或 TOTP 包。
