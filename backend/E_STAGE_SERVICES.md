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

## E5 文件生命周期

StorageService.upload 仅允许已升级站长，支持非空、最多 20 MiB 的 PDF / DOCX；
扩展名、内容签名、MIME、DOCX 展开大小一起校验。请求身份由账号与幂等键生成稳定 UUID，
对象 key 从 staging 到正式区保持不变。相同键不同内容冲突，ready 重试直接返回原记录。
校验及物理写入在短鉴权事务之外，随后重新鉴权，FileObject(staged) 与 finalize Job 同事务登记。

finalize / run_delete 是供 H 执行器调用的任务处理入口：两端均复核当前站长与有效 lease，
中间进行物理移动/删除；末端文件状态与 Job.finish 同一事务。物理操作可重复，租约丢失
不提交正式状态；物理成功而数据库失败时重试收敛。运行循环、退避和租约回收在 H 接入。
delete 先记录 pending_delete 并阻断读取；关联资源必须提供双版本，同时撤销投影及权限 epoch。
删除磁盘失败保留 pending_delete。read_private 两端检查身份、文件和关联资源，返回前校验摘要。

cleanup_orphans 接受明确时区时钟，仅移除超过 24 小时且没有数据库登记的 staging；
已登记文件由任务流程收敛，不按年龄删除。LocalObjectStore 限制规范对象 key、解析后路径
必须位于 storage_root 内；通过 ContextVar 阻止在活跃 UoW 内调用对象存储。
基础验收 3 项：上传/转正/重试/权限、删除失败重试与立即阻断、孤儿回收保留登记对象。
