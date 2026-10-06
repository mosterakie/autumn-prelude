# 05 运行配置与验收

本节供恢复本地研究环境使用。以下是说明命令，本轮整理没有执行启动、迁移、站长引导、发信或真实供应商测试。代码快照不携带业务库、上传目录和私人配置。

## 运行所需组件

| 组件     | 开发环境                                                       |
| -------- | -------------------------------------------------------------- |
| Python   | 项目要求 3.12+；需安装 backend 包与 dev/agent/providers extras |
| Node     | 22.12+；原运行说明建议 24 LTS                                  |
| 数据库   | PostgreSQL + pgvector；本机容器端口 5442，应用库 autumn        |
| 前端     | 3000；Next 同源转发后端 API                                    |
| API      | 127.0.0.1:8000；Uvicorn FastAPI factory                        |
| Worker   | 独立后端进程；领取任务、处理模型/邮件和维护                    |
| 框架状态 | 官方 Postgres saver，独立 autumn_checkpoints schema            |
| 文件     | AUTUMN_STORAGE_ROOT，默认 backend/var/storage；不包含在归档    |

配置默认 DSN 与本机实际端口并不相同，恢复时必须显式填写 5442 和正确库名。业务库与测试库必须分开，不能把会提交并发数据的用例指向业务库。

## 配置位置

| 文件                           | 内容                                          | 归档情况                 |
| ------------------------------ | --------------------------------------------- | ------------------------ |
| `frontend/.env.local`          | API 模式、FASTAPI_ORIGIN、DEV_ALLOWED_ORIGINS | 本地忽略，不归档         |
| `backend/.env`                 | 数据库、环境、会话/CSRF/加密密钥、可信来源等  | 本地忽略，不归档         |
| `backend/.env.providers.local` | 模型、嵌入与搜索配置/凭据                     | 本地忽略，不归档         |
| `backend/.env.mail.local`      | SMTP、发件信息、授权码、前端链接地址          | 本地忽略，不归档         |
| 两端 `.env.example`            | 变量名与占位值                                | 包含模板，不包含当前凭据 |

后端按 `.env → .env.providers.local → .env.mail.local` 读取，同名值后者覆盖；进程环境变量优先。API 和 Worker 必须读取一致的数据库、认证加密密钥和相关配置。改变配置后重启使用它的进程。

当前供应商接入记录为 DeepSeek/deepseek-flash、百炼/text-embedding-v4（1024 维）、Tavily/basic。这是代码和 2026-10-06 验收的选择，恢复后可用型号/工作空间仍需按账号配置核对。百炼使用已配置的工作空间兼容地址，不在文档中复制私人工作空间 URL 和密钥。

## 启动顺序

安装后端依赖，在 backend 目录：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,agent,providers]"
```

数据库结构需在明确目标环境下应用业务迁移；框架表首次初始化走独立命令。它们会写数据库，不属于日常只读自检：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m autumn_backend.workers --setup-checkpoints
```

分别打开 API 与 Worker：

```powershell
.\.venv\Scripts\python.exe -m uvicorn autumn_backend.app:create_app --factory --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m autumn_backend.workers
```

API 负责受理，Worker 执行已配置任务。Worker 和 `--once` 都会实际领取并执行任务；不能把它们用作无副作用健康检查。未配置相关端口时不会返回虚构模型/邮件成功。

前端目录：

```powershell
npm ci
npm run dev -- --hostname 0.0.0.0
```

真实数据配置：

```dotenv
NEXT_PUBLIC_API_MODE=api
FASTAPI_ORIGIN=http://127.0.0.1:8000
```

这些命令中的 python 应使用实际安装后端依赖的解释器；当前工作区的审计虚拟环境属于本机验证辅助，不是必须依赖的固定项目路径。

## 手机同局域网

前端填写电脑的确切主机 IP，不含协议和端口：

```dotenv
DEV_ALLOWED_ORIGINS=<电脑IPv4>
```

后端配置可信前端来源，包含协议和端口：

```dotenv
AUTUMN_TRUSTED_ORIGINS='["http://localhost:3000","http://127.0.0.1:3000","http://<电脑IPv4>:3000"]'
```

前端监听 0.0.0.0 后，手机打开 `http://<电脑IPv4>:3000/`。API 可以仍监听回环地址，手机经 Next 转发访问。IP 改变后更新两处并重启 API/前端；不要把可信来源和开发允许主机设成无条件接受。

## 邮箱配置

使用站长指定的 163 邮箱，SMTP 服务器 smtp.163.com、465 隐式 TLS；只发信，不需 POP3/IMAP。授权码由站长填写在 `backend/.env.mail.local` 的 `AUTUMN_SMTP_PASSWORD`，完整配置见 [邮箱说明](../../backend/EMAIL_CONFIGURATION.md)。本轮不读取或归档授权码。

`AUTUMN_FRONTEND_BASE_URL` 是收件人打开前端的地址。手机打开邮件时 localhost 指向手机，应改成手机可访问的电脑地址，后续正式运行改为 HTTPS 域名。配置读取后重启 API 和 Worker。

API 受理成功、任务发送成功和最终收件是不同观察点。先确认处理器已配置，再申请一条受控测试邮件，核对任务/安全错误码、收件箱、链接有效性和一次性消费。该真实链路尚未作为项目完成验收。

## 普通账户与站长验证

公开注册只能创建 member。真实账号密码为 12–256 字符；邮箱验证令牌有效 24 小时，重置有效 1 小时。代码默认会话绝对期限 720 小时、空闲期限 24 小时；业务保存永久与这些期限无关。

站长引导为本地交互命令，只有明确需要新建首位站长时运行：

```powershell
.\.venv\Scripts\python.exe -m autumn_backend.cli bootstrap-owner
```

流程需要密码、TOTP 绑定和当前代码，验证成功后创建 owner 与因子；已有普通邮箱不会被自动提升。引导 URI 与恢复码不进入归档。密码登录后仍需 step-up，邮箱发件配置不会授予站长身份。

`visitor@example.com`、`owner@example.com` 和验证码 `123456` 仅属于 demo，不保证真实业务库存在这些账号。本次没有盘点、复制或导出业务账户。

## 基础验证方法与历史结论

| 验证                          | 能证明什么                         | 不能单独证明什么                 |
| ----------------------------- | ---------------------------------- | -------------------------------- |
| TypeScript、Ruff、mypy        | 类型和部分静态错误                 | 完整业务成功                     |
| Alembic check                 | 可比较结构无漂移                   | 所有 CHECK、触发器和并发语义     |
| Catalog/约束探针              | 数据库结构及高风险行为             | 浏览器整个用户旅程               |
| 独立 PostgreSQL 服务/并发用例 | 特定事务、锁、隔离和状态转换       | 所有供应商异常                   |
| 真实 AI 两条有限链路          | 当时模型/向量/搜索和正式结果可串联 | 全站、邮件、每种自然语言工具表达 |
| 390px 浏览器检查              | 当时布局和指定交互                 | 所有手机与浏览器内核             |

本轮仅做文档链接、静态清单和归档完整性检查；历史测试计数见 [09 证据索引](09-evidence-index.md)。真实供应商用例默认跳过，显式开启会产生费用，归档整理没有触发它们。

## 常见问题定位

| 现象                     | 优先核对                                                 |
| ------------------------ | -------------------------------------------------------- |
| 页面能浏览，提交 403     | 后端可信 Origin、当前 CSRF、会话轮换、站长 step-up       |
| Run 一直 queued          | Worker 是否运行，真实模型/知识/saver 是否装配            |
| 注册受理却没邮件         | SMTP 授权码、auth.email 注册、Worker、令牌是否过期       |
| 无联网工具               | owner 会话、当前 TOTP、web/auto、Tavily 配置             |
| 收藏只给文字描述         | 是否产生真实 Action 预览，不以模型声明代替保存           |
| 补充回答参数不对         | 选项原值、等待项是否过期、是否混用操作确认               |
| 工作台知识/设置请求 404  | 当前尚缺对应 HTTP 入口，参见路由快照                     |
| provider outcome unknown | 保留账本，查回执后人工对账，不自动重发                   |
| 文件/索引 waiting_auth   | 当前站长验证后通过限定 jobs resume，不随意换绑已确认动作 |

重试应保留原请求键，不以修改未知账本、清空表或重复发问来伪造成功。当前部署仍暂缓。
