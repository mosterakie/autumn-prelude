# 纯权限判定契约（D）

## 身份上下文与三个入口（D1）

ActorContext 为 frozen dataclass，包含 user_id、role、auth_session_id、
step_up_expires_at、不可变 capabilities 和 scope_epoch。匿名身份不携带账号、
会话或升级验证时间；认证身份必须同时有账号与会话。时间必须带时区。

三个入口在所属阶段实现，本阶段只规定装配接口：

| 入口 | 服务端装配依据 | 禁止作为授权来源 |
| --- | --- | --- |
| API（F） | Cookie 令牌哈希对应的当前会话、账号与 ACL epoch | 请求中的 role、owner_id、capabilities |
| Agent（G） | Run.auth_session_id 对应的当前会话、账号与 ACL epoch | checkpoint 保存的旧身份、模型生成的工具参数 |
| Worker（H） | Job 绑定的 Run / auth_session_id / actor_id 及当前记录 | 浏览器 Cookie 明文、Job JSON 内声称的权限 |

站长完成升级后会话可能轮换。恢复入口先校验当前身份与原 Run 属于同一用户，
再在受控事务中重新绑定会话；不得通过 checkpoint 或工具参数自行替换身份。
无用户会话的系统维护作业不能伪装成站长，应由专用内部 handler 和服务处理。

capabilities 是 UI 和工具展示提示，绝不是可回传的授权凭据。后续判定重新检查
服务端当前事实和角色。frozen 仅防止代码意外修改，不能替代身份认证。
纯词表不导入 db/enums，因为该模块依赖 SQLAlchemy；入口显式转换经过验证的值。

HTTP 认证、工具入口与 Worker 装配尚未实现，现有类型不能被描述为这些入口已上线。

## 权限事实（D2）

Service 在当前事务或读取批次中组装 PolicyFacts。now 必须是明确传入的带时区时间，
current_scope_epoch 来自当前设置；判定内部不读时钟或环境。AuthenticationFacts
同时包含当前账号、会话、版本、撤销、绝对/闲置到期和升级时间。
ResourceFacts 包含当前资源生命周期及 PublicationFacts，不读取原稿正文；
publication.revision_id 可以是已发布的旧原稿，不能误认为必须等于最新私人原稿。
TargetFacts 保存会话、运行、引用、留言、动作或记忆的归属和必要模式。
引用的 owner_id 是 Run 用户；关联资料另用 resource_id 和 ResourceFacts 检查。

所有嵌套结构均不可变；不接受 dict、ORM 行或无时区时间替代。不存在对象表示为
None，不能因省略 facts 而默认授权。关联文章的留言必须装配对应资源；resource_id
为 None 的留言是留言板。资源与公开投影、对象与相关资源必须一致。
简单场景由 Service 直接装配；复杂只读 loader 如将来需要，放在独立模块，不能
被纯判定代码导入，也不能写库、提交事务或成为另一套业务服务（可选 D5）。

## 纯判定与结果（D3）

调用形式固定为 evaluate(actor, facts) → Decision。判定只有传入值计算，不查询
数据库、读取配置、调用网络或隐式读时钟。Decision 包含 allowed、错误码和状态码，
冷却错误可带 retry_at；无权对象不返回归属、版本或存在性细节。

公开读取只接受当前未撤回投影和未删除、归档或到期资源；原件另需允许下载。
更新私人原稿不会使仍有效的旧公开投影失效。留言须审核通过，关联文章仍须公开。
public 模式的聊天仍只属于其用户；站长没有跨账号读取特权。归属先过滤，
缺失、跨账号对象返回同一个 NOT_FOUND，本人失效会话返回 SESSION_EXPIRED。
owner 模式及本人私人对象重新检查两份升级时间；等于截止时间即失效。
认证事实中的角色、账号和会话绑定、身份版本、撤销和闲置/绝对期限都须有效。
匿名公开读取不因浏览器携带的账号状态改变，但保护操作必须验证当前认证。

## 角色与工具矩阵（D4）

| 当前身份 | 展示能力 | 附加前置条件 |
| --- | --- | --- |
| anonymous / 失效会话 | read_public | 现行公开投影、审核通过留言 |
| 有效未验证账号 | read_public、own_chat | 聊天始终检查本人归属 |
| 已验证 member 或未升级 owner | 上述 + public_ai、write_comment | 新 ask 检查冷却；Service 原子检查额度、速率、并发 |
| 已验证且升级有效 owner | 上述 + private_knowledge、search_web、manage_content、manage_site、own_action、own_memory | 私人资料、动作、记忆仍只限本人；管理元数据不包括他人聊天 |

capabilities_for 重新计算展示提示，不读取 Actor.capabilities。即使回传全部能力也
不能让 member 联网。能力提示不等同余额或对象授权；每项操作仍须 evaluate。
public / auto 问答不会授予联网能力；显式 web 仅限已升级站长。
新的 ask 要求邮箱验证、会话归属、固定模式和冷却完成；截止时间相等允许受理。
幂等重放先校验当前身份/会话模式与来源，再返回原结果，不能调用新 ask 冷却判断。
恢复和已受理任务内工具不重复检查新 ask 冷却；余额、并发槽、幂等及计数仍由 E 处理。

选择的资源 ID 不是授权。Service 对 resource_ids 逐项组装 facts 和校验；指定 ID
却未找到资源须保留 requested_resource_id，不能退化为检索全部资料或留言板。
留言作者可改删本人留言；已升级站长可审核删除他人留言，但不能替作者改写正文。
pending 父留言只允许作者本人回复，已审核父留言可按其公开关联范围回复。

Action.requires_step_up 由 Service 根据受控动作类型装配，不来自模型或 checkpoint。
已过期 action 可以检查真实状态，但不能因此绕过 Service 的到期/状态/确认/双版本
校验。权限判定成功不等同允许执行副作用。HTTP 和工具入口、提交事务均须重建并检查。

## 来源与上下文失效（D6）

RESUME_RUN、CONTINUE_RUN 和 EMIT_RUN_OUTPUT 先验证当前身份、运行归属和固定模式，再检查
ContextFacts。缺少上下文、依赖闭包未完整装配、Run/模式不匹配、捕获的 epoch
或执行 generation 与当前不一致，均返回 ACL_CONTEXT_INVALIDATED。
Actor.scope_epoch 也须等于当前 epoch，防止旧身份快照混入新的执行。
全站 epoch 变化先保守停止旧上下文，即使来源当前仍可读或撤回后再次公开。

sources 包含全部实际送入模型的来源，包含历史回答、摘要和记忆的传递依赖，
不局限于最终答案引用。依赖未找到资源/原稿、到期、删除、归档、ACL 变化，
或公开来源的 publication/revision 不再匹配、AI 开关关闭，都阻止继续输出。
即使 epoch 未递增，到期时间到达仍会实时阻断。
SourceFacts 校验原稿与资源绑定；私人旧原稿不必是最新原稿，权限有效时仍可用。
private chunk 后来对应已公开资源也不能改称 public；公开模式只能用公开投影片段。
联网来源属于原 Run 用户，当前访问仍须站长升级验证；验证失效返回 STEP_UP_REQUIRED。
引用必须匹配其持久 source_id，不能用新 publication 的正文替代已捕获来源。
READ_RUN 允许读取合法归属的状态元数据，正文、引用和事件快照另走上下文/来源判定，
不能因状态读取获准便原样返回全部历史文本。缺少被请求的父留言须保留
requested_parent_id，不能把回复请求退化成顶层留言。

执行器收到 ACL_CONTEXT_INVALIDATED 后的处理契约：

1. 停止输出旧上下文文本，并尽力取消当前 provider stream。
2. 用现有 source.invalidated 隐藏受影响消息，用 scope.changed 通知范围变化。
3. 合法事务中结束旧执行阶段，Run 为 failed/cancelled，error_code 为
   ACL_CONTEXT_INVALIDATED；error/done 使用既有 SSE 词表。
4. 继续时重新鉴权、读取获准原始资料并重建提示词/历史/摘要/记忆，再创建新的
   受控执行阶段。不能只修改 checkpoint 的 epoch/generation 数字后重用旧文本。
5. 旧 provider 回调、Job 结果和 SSE 快照不得提交；同事务校验租约、generation、
   当前权限后才可写正式结果。此前已发送字节无法收回。

v1.1 实施建议中的 run.invalidated 在这里表示逻辑失效，不增加同名 SSE 事件
或 Run 状态：数据库/接口已冻结的事件是 source.invalidated 与 scope.changed。
目前 Run 尚无持久 execution generation 字段；本阶段只交付其纯值判定契约。
G/H 仍须实现来源闭包装配、持久代际、流取消和联合事务闸门，不能把纯判定通过
描述为已完成运行时安全。sources_complete 只能由实际完成装配的服务设置。

## 验证与后续装配

纯判定可在 Python -I -S（不加载第三方包）中导入并运行。测试在判定期间阻断
文件/网络访问，并检查不读隐式时钟或环境；另比对纯词表与当前 DB 枚举。
测试包括跨账号、升级/会话/冷却截止、能力回传、公开投影、来源撤回和旧代际。
可选 D5 loader 本阶段没有需求，不创建空装配层；E 在短事务内直接装配事实。
需要多表依赖装配时再增加独立只读 loader，核心不得依赖它。
