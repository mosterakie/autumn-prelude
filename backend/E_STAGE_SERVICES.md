# E 阶段服务契约

E1 发布、E2 问答受理、E3 留言提交分别见现有服务契约。这里记录 E4–E8 的服务入口、
事务边界和后续 F/G/H 接入责任。每个节点仅做必要的基础集成验收，本地提交，不推送。

## E4 额度结算

QuotaService.current 根据当前时间/角色配置装配今日额度，只读，不为查询创建新桶。
结算方法以 `_in_uow` 结尾，供可信执行器组合进同一个 UoW，不自行提交或执行外部调用。
执行器必须先完成当前权限、job lease 和 run generation 检查；实际模型网络请求发生在提交之后。

- dispatch_model_in_uow：校验调用属于 Run 且用途是 chat/tool；prepared→dispatched
  标记与 reserved→charged 同事务。每个 Run 只扣一次；重试不产生第二次扣次。
- release_before_dispatch_in_uow：只允许未派发模型调用的预留释放。调用后停止、结果不明
  不能走此路径；账本仍记录 unknown，不能当作零成本自动重发。
- refund_failure_in_uow：仅接受服务端可信故障分类；幂等 charged→refunded，账本不删除。
  这是内部编排入口，不是开放给 HTTP 或模型参数的退款工具。
- cleanup_candidates：仅返回老旧 reserved 且 Run 已失败/取消、无模型派发的候选 ID。
  F/H 调度需逐个重新锁定/复核后释放；本节点不启动清理，不按年龄释放活跃任务。

日桶一直使用 Reservation.bucket_id，不随午夜、配置修改或恢复改绑。
锁顺序为 user→conversation→run→provider_call→bucket→reservation；上层联合结果提交须遵循相同顺序。
基础验收：2 项真实 PostgreSQL 流程通过，覆盖派发扣次/停止/故障退款、调用前释放与晚期故障回滚。
