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
