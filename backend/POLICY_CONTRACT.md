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
| Worker（H） | Job 绑定的 Run / auth_session_id / actor_user_id 及当前记录 | 浏览器 Cookie 明文、Job JSON 内声称的权限 |

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
