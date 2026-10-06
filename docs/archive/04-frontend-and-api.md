# 04 前端与接口边界

## 技术与模式

前端使用 Next.js App Router、React、TypeScript、Tailwind CSS、TanStack Query、react-markdown 和 lucide-react。精确依赖见 [package.json](../../frontend/package.json) 与 lock 文件；浏览器交互不使用 Next Server Actions 实现业务。

`NEXT_PUBLIC_API_MODE=api` 时，公开 SSR 从 FastAPI 读取，客户端通过同源 `/api/*` 由 Next 转发。API 失败显示真实错误，不回退 demo。只有 demo 模式才动态加载内存演示适配器，示例登录、模型回复和队列进度没有业务持久性。

## 页面与真实接口

| 页面                                                   | 当前接口/能力                             | 联调边界                                                       |
| ------------------------------------------------------ | ----------------------------------------- | -------------------------------------------------------------- |
| `/`、`/notes`、`/notes/[slug]`、`/bookmarks`、`/about` | public/site、公开投影列表/详情            | 首页和关于的展示内容部分固定，主题和上传上限未完全服务端化     |
| `/guestbook`                                           | 公开留言、提交、举报、审核                | 新留言默认 pending；提交成功不等于立即公开                     |
| `/login`、`/register` 等                               | auth 邮箱密码、令牌、me、step-up          | 实际收件和完整验证/重置浏览器链路待验收                        |
| `/account`                                             | 本人当前身份、额度和退出                  | 真实身份来自服务端，demo 邮箱不能充当真实测试账户              |
| `/chat`、`/chat/[id]`                                  | 本人 public 模式会话、ask、Run、SSE、引用 | 仅公开 AI 资料，不联网、不公开聊天                             |
| `/admin/chat`、`/[id]`                                 | 当前额外验证后的 owner 会话               | 默认 auto，可选站内/联网；还需供应商和 Worker 配置             |
| `/admin/content`                                       | resources CRUD、发布预览、确认、撤回      | 幂等键和目标双版本受后端裁决                                   |
| `/admin/moderation`                                    | 留言/举报审核                             | 只读必要审核信息，不因此获得他人聊天                           |
| `/admin/knowledge`                                     | 文件/URL 表单和任务卡片已实现             | 上传、URL 导入及通用任务 HTTP 尚缺，demo 可展示不等于 API 可用 |
| `/admin/settings`                                      | AI 限额、保留、记忆、审计界面             | 多个管理 HTTP 尚未注册，部分动作服务可通过 Agent 预览          |

后台的 owner jobs 与 provider-calls 对账接口已有代码；前端尚未覆盖完整运维工作流。现有路由全集以 [08 快照](08-api-snapshot.md) 为准，原 [API 契约](../architecture/api-contract.md) 同时包含规划入口。

## 身份、缓存与写请求

- Cookie 会话令牌 HttpOnly，前端通过 `/auth/me` 取得身份与内存 CSRF；不在 localStorage 保存登录令牌。
- 已认证写请求携带 Cookie、X-CSRF-Token 和所需 Idempotency-Key；服务端重新验证 Origin、当前会话、对象归属和版本。
- 查询键按账号和模式隔离；退出关闭 SSE、清除敏感缓存，额外验证到期卸载管理视图。
- BroadcastChannel 不可用时，存储事件传播退出标记；标记只有时间戳，身份仍由服务端检查。存储也被禁用时依赖周期身份查询及后端授权。
- 页面隐藏按钮、system prompt、客户端 role 值及旧能力提示都不能赋予权限。

## SSE 与状态展示

`POST /api/ask` 返回 202、Run 和事件地址。客户端读取当前状态和累计消息快照，用 `content_version` 替换整段正文。事件只追加、按 Run 序号排序；重连使用 after 或 Last-Event-ID，两者同时存在时必须一致。

断线不会取消 Run，也不重新扣次。等待输入、审批或认证时停止自动重连，用户完成对应操作后读取同一 Run 再连接。取消必须发送明确 cancel 请求。

服务端逐批重验身份和完整来源。`source.invalidated` 或 `scope.changed` 让客户端隐藏失权文本并重读状态。已经发出的字节不能从用户设备收回，因此不能承诺“撤回让曾经看过的人忘记”。

状态与头像相对应，composing/等待/执行/完成等视觉反馈来自实际状态。当前没有供应商逐 token 流式输出；有 SSE 传输层不代表模型已经逐字生成。

## 预览、补充与确认

Action 预览显示真实对象、标题/正文/URL/标签、变更范围和目标版本。确认请求只传既有 Action 版本与 parameters_hash，不能替换目标或参数。202 通常表示 ready/已排队，最终结果还需 Worker 结算。

InputRequest 有 options 时显示单选并提交原值，没有 options 才显示文字框。答案失败保留输入，重试保留原答案身份；到期禁用并提供停止。补充与审批是不同状态，不能输入“继续”来绕过真正的执行确认。

新建手记/收藏默认私人，公开字段单独设置。公开原文件、正文、摘要、备注、标题和链接不是同一个开关，不能从“公开收藏”推导“公开所有抓取材料”。

## 交互修复与当前视觉

登录弹窗仅在挂载时设置焦点和滚动锁；计时器更新不再重复夺取焦点。跳转注册页时关闭旧弹窗并恢复滚动。页头登录是实际链接，手机菜单使用原生 details/summary；脚本可用时增加外部点击、Escape 和导航关闭。

局域网 HTTP 缺少 randomUUID 时使用安全随机字节生成 UUID；真实 API 不在启动时初始化依赖 structuredClone 的 demo。主页视差兼容旧媒体监听 API，触屏保持静态。

主页立绘与聊天透明像素 PNG 使用停云方向。聊天桌面 96×112 px，手机 72×84 px，可收起。鼠标视差主图幅度 12×9 px、圆盘反向 7×6 px、星点 18×12 px，文字与入口卡片稳定，离开回正；减少动画条件下禁用。

概念稿曾使用“青序”和阮梅青玉色系，属于历史比较方案。当前业务品牌是秋序，已实现主题是停云圆角方案，原型图片不能作为像素级实现验收。

## 需要保留的测试认识

早期前端有 23 项 Vitest 与生产构建通过记录；后续各节点仅做针对性基础检查。最近手机修复完成类型/格式检查、7 项接口与 UUID 测试和 390px 本地浏览器验证。真实手机 Vim 的复测仍待服务重启后完成，不能把缩窄桌面浏览器当作所有手机内核的兼容证明。

接口 DTO 当前仍由前端 contracts 手写维护，不宣称已建立完整 OpenAPI 自动生成链。后续要做字段、nullable、错误、版本、等待状态及尚缺路由的联合契约核对。
