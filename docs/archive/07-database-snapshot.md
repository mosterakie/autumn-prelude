# 07 数据库字段与约束快照

归档日期：2026-10-06。由当前 SQLAlchemy 模型静态导出；没有连接数据库，也没有读取业务记录。
这份快照说明代码所声明的结构，不能证明本机或其他环境已经应用全部迁移。
迁移 DDL、触发器和数据库行为还需结合原始数据库设计、迁移文件及 Catalog/语义验收阅读。

应用模型共 **28 张表**；不含 LangGraph 独立 schema 的框架表。

## 表索引

- [actions](#actions)
- [admin_factors](#admin_factors)
- [audit_events](#audit_events)
- [auth_sessions](#auth_sessions)
- [auth_tokens](#auth_tokens)
- [comments](#comments)
- [conversation_summaries](#conversation_summaries)
- [conversations](#conversations)
- [file_objects](#file_objects)
- [jobs](#jobs)
- [knowledge_chunks](#knowledge_chunks)
- [knowledge_indexes](#knowledge_indexes)
- [memories](#memories)
- [messages](#messages)
- [provider_calls](#provider_calls)
- [publications](#publications)
- [quota_buckets](#quota_buckets)
- [quota_reservations](#quota_reservations)
- [rate_limit_buckets](#rate_limit_buckets)
- [reports](#reports)
- [resource_versions](#resource_versions)
- [resources](#resources)
- [retention_policies](#retention_policies)
- [run_events](#run_events)
- [run_sources](#run_sources)
- [runs](#runs)
- [settings](#settings)
- [users](#users)

## actions

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 352 行。

| 列                         | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| -------------------------- | -------------------------- | ---- | ---- | ----------------- |
| `actor_id`                 | `UUID`                     | 否   | 否   | —                 |
| `auth_session_id`          | `UUID`                     | 是   | 否   | —                 |
| `run_id`                   | `UUID`                     | 是   | 否   | —                 |
| `authorization_message_id` | `UUID`                     | 是   | 否   | —                 |
| `type`                     | `VARCHAR(32)`              | 否   | 否   | —                 |
| `target_resource_id`       | `UUID`                     | 是   | 否   | —                 |
| `expected_version`         | `BIGINT`                   | 是   | 否   | —                 |
| `expected_acl_version`     | `BIGINT`                   | 是   | 否   | —                 |
| `parameters`               | `JSONB`                    | 否   | 否   | —                 |
| `parameters_hash`          | `TEXT`                     | 否   | 否   | —                 |
| `authorization_kind`       | `VARCHAR(32)`              | 否   | 否   | —                 |
| `status`                   | `VARCHAR(32)`              | 否   | 否   | —                 |
| `requires_confirmation`    | `BOOLEAN`                  | 否   | 否   | —                 |
| `idempotency_key`          | `VARCHAR(128)`             | 否   | 否   | —                 |
| `expires_at`               | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —                 |
| `confirmed_at`             | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `executed_at`              | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `result`                   | `JSONB`                    | 是   | 否   | —                 |
| `error_code`               | `VARCHAR(64)`              | 是   | 否   | —                 |
| `id`                       | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`               | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`               | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`                  | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_actions_authorization_kind_valid`：`authorization_kind IN ('explicit_request', 'confirmed_preview')`
- CHECK `ck_actions_confirmed_statuses_require_confirmed_at`：`status NOT IN ('ready', 'running', 'succeeded', 'failed') OR confirmed_at IS NOT NULL`
- CHECK `ck_actions_executed_at_matches_status`：`(status = 'succeeded') = (executed_at IS NOT NULL)`
- CHECK `ck_actions_expected_acl_version_non_negative`：`expected_acl_version IS NULL OR expected_acl_version >= 0`
- CHECK `ck_actions_expected_version_non_negative`：`expected_version IS NULL OR expected_version >= 0`
- CHECK `ck_actions_expires_after_created`：`expires_at > created_at`
- CHECK `ck_actions_parameters_hash_not_empty`：`length(parameters_hash) > 0`
- CHECK `ck_actions_parameters_is_object`：`parameters IS NULL OR jsonb_typeof(parameters) = 'object'`
- CHECK `ck_actions_status_valid`：`status IN ('proposed', 'awaiting_confirmation', 'ready', 'running', 'succeeded', 'failed', 'cancelled', 'expired')`
- CHECK `ck_actions_type_valid`：`type IN ('publish', 'create_resource', 'update_resource', 'revoke', 'delete', 'update_settings', 'provide_input', 'create_memory', 'update_memory', 'delete_memory', 'apply_retention')`
- CHECK `ck_actions_unconfirmed_has_no_confirmed_at`：`status NOT IN ('proposed', 'awaiting_confirmation') OR confirmed_at IS NULL`
- FK `fk_actions_actor_id_users`：`(actor_id) → (users.id)`；ON DELETE CASCADE
- FK `fk_actions_auth_session_id_auth_sessions`：`(auth_session_id) → (auth_sessions.id)`；ON DELETE SET NULL
- FK `fk_actions_authorization_message_id_messages`：`(authorization_message_id) → (messages.id)`；ON DELETE SET NULL
- FK `fk_actions_run_id_runs`：`(run_id) → (runs.id)`；ON DELETE CASCADE
- FK `fk_actions_target_resource_id_resources`：`(target_resource_id) → (resources.id)`；ON DELETE CASCADE
- UNIQUE `uq_actions_actor_id_idempotency_key`：`(actor_id, idempotency_key)`

### 显式索引

- `ix_actions_run_id_status`：`(actions.run_id, actions.status)`
- `ix_actions_status_expires_at`：`(actions.status, actions.expires_at)`
- `ix_actions_target_resource_id`：`(actions.target_resource_id)`

## admin_factors

模型：[源文件](../../backend/src/autumn_backend/db/models/identity.py)，类定义位于第 189 行。

| 列                       | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------------ | -------------------------- | ---- | ---- | ----------------- |
| `user_id`                | `UUID`                     | 否   | 否   | —                 |
| `kind`                   | `VARCHAR(16)`              | 否   | 否   | —                 |
| `secret_ciphertext`      | `TEXT`                     | 否   | 否   | —                 |
| `encryption_key_version` | `BIGINT`                   | 否   | 否   | —                 |
| `last_used_time_step`    | `BIGINT`                   | 是   | 否   | —                 |
| `recovery_code_hashes`   | `JSONB`                    | 是   | 否   | —                 |
| `enabled_at`             | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `revoked_at`             | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`                     | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`             | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`             | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`                | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_admin_factors_encryption_key_version_positive`：`encryption_key_version >= 1`
- CHECK `ck_admin_factors_kind_valid`：`kind IN ('totp')`
- CHECK `ck_admin_factors_secret_ciphertext_not_empty`：`length(secret_ciphertext) > 0`
- FK `fk_admin_factors_user_id_users`：`(user_id) → (users.id)`；ON DELETE CASCADE
- UNIQUE `uq_admin_factors_user_id`：`(user_id)`

### 显式索引

无额外显式索引；主键和 UNIQUE 仍会建立相应索引。

## audit_events

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 738 行。

| 列               | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ---------------- | -------------------------- | ---- | ---- | ----------------- |
| `actor_id`       | `UUID`                     | 是   | 否   | —                 |
| `action_id`      | `UUID`                     | 是   | 否   | —                 |
| `resource_id`    | `UUID`                     | 是   | 否   | —                 |
| `event_type`     | `TEXT`                     | 否   | 否   | —                 |
| `result`         | `VARCHAR(16)`              | 否   | 否   | —                 |
| `before_version` | `BIGINT`                   | 是   | 否   | —                 |
| `after_version`  | `BIGINT`                   | 是   | 否   | —                 |
| `request_id`     | `TEXT`                     | 是   | 否   | —                 |
| `metadata_json`  | `JSONB`                    | 是   | 否   | —                 |
| `expires_at`     | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`             | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `created_at`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_audit_events_after_version_non_negative`：`after_version IS NULL OR after_version >= 0`
- CHECK `ck_audit_events_before_version_non_negative`：`before_version IS NULL OR before_version >= 0`
- CHECK `ck_audit_events_event_type_shape`：`event_type ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'`
- CHECK `ck_audit_events_metadata_is_object`：`metadata_json IS NULL OR jsonb_typeof(metadata_json) = 'object'`
- CHECK `ck_audit_events_result_valid`：`result IN ('succeeded', 'failed', 'denied')`
- FK `fk_audit_events_action_id_actions`：`(action_id) → (actions.id)`；ON DELETE SET NULL
- FK `fk_audit_events_actor_id_users`：`(actor_id) → (users.id)`；ON DELETE SET NULL
- FK `fk_audit_events_resource_id_resources`：`(resource_id) → (resources.id)`；ON DELETE SET NULL

### 显式索引

- `ix_audit_events_action_id`：`(audit_events.action_id)`
- `ix_audit_events_actor_id_created_at`：`(audit_events.actor_id, audit_events.created_at)`
- `ix_audit_events_event_type_created_at`：`(audit_events.event_type, audit_events.created_at)`
- `ix_audit_events_resource_id_created_at`：`(audit_events.resource_id, audit_events.created_at)`

## auth_sessions

模型：[源文件](../../backend/src/autumn_backend/db/models/identity.py)，类定义位于第 106 行。

| 列                    | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| --------------------- | -------------------------- | ---- | ---- | ----------------- |
| `user_id`             | `UUID`                     | 否   | 否   | —                 |
| `token_hash`          | `TEXT`                     | 否   | 否   | —                 |
| `csrf_version`        | `BIGINT`                   | 否   | 否   | —                 |
| `auth_version`        | `BIGINT`                   | 否   | 否   | —                 |
| `last_seen_at`        | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `idle_expires_at`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —                 |
| `absolute_expires_at` | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —                 |
| `step_up_expires_at`  | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `revoked_at`          | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`                  | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`             | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_auth_sessions_absolute_expires_after_created`：`absolute_expires_at > created_at`
- CHECK `ck_auth_sessions_auth_version_positive`：`auth_version >= 1`
- CHECK `ck_auth_sessions_csrf_version_positive`：`csrf_version >= 1`
- CHECK `ck_auth_sessions_idle_within_absolute_expiry`：`idle_expires_at <= absolute_expires_at`
- FK `fk_auth_sessions_user_id_users`：`(user_id) → (users.id)`；ON DELETE CASCADE
- UNIQUE `uq_auth_sessions_id_user_id`：`(id, user_id)`
- UNIQUE `uq_auth_sessions_token_hash`：`(token_hash)`

### 显式索引

- `ix_auth_sessions_absolute_expires_at`：`(auth_sessions.absolute_expires_at)`
- `ix_auth_sessions_idle_expires_at_active`：`(auth_sessions.idle_expires_at)`；谓词 `revoked_at IS NULL`
- `ix_auth_sessions_user_id`：`(auth_sessions.user_id)`

## auth_tokens

模型：[源文件](../../backend/src/autumn_backend/db/models/identity.py)，类定义位于第 158 行。

| 列              | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| --------------- | -------------------------- | ---- | ---- | ----------------- |
| `user_id`       | `UUID`                     | 否   | 否   | —                 |
| `purpose`       | `VARCHAR(32)`              | 否   | 否   | —                 |
| `token_hash`    | `TEXT`                     | 否   | 否   | —                 |
| `expires_at`    | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —                 |
| `consumed_at`   | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `attempt_count` | `BIGINT`                   | 否   | 否   | —                 |
| `id`            | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`    | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`    | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`       | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_auth_tokens_attempt_count_non_negative`：`attempt_count >= 0`
- CHECK `ck_auth_tokens_expires_after_created`：`expires_at > created_at`
- CHECK `ck_auth_tokens_purpose_valid`：`purpose IN ('verify_email', 'reset_password')`
- FK `fk_auth_tokens_user_id_users`：`(user_id) → (users.id)`；ON DELETE CASCADE
- UNIQUE `uq_auth_tokens_token_hash`：`(token_hash)`

### 显式索引

- `ix_auth_tokens_expires_at`：`(auth_tokens.expires_at)`
- `ix_auth_tokens_user_id_purpose`：`(auth_tokens.user_id, auth_tokens.purpose)`

## comments

模型：[源文件](../../backend/src/autumn_backend/db/models/content.py)，类定义位于第 442 行。

| 列             | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| -------------- | -------------------------- | ---- | ---- | ----------------- |
| `author_id`    | `UUID`                     | 否   | 否   | —                 |
| `resource_id`  | `UUID`                     | 是   | 否   | —                 |
| `parent_id`    | `UUID`                     | 是   | 否   | —                 |
| `client_id`    | `UUID`                     | 否   | 否   | —                 |
| `body`         | `TEXT`                     | 否   | 否   | —                 |
| `request_hash` | `TEXT`                     | 否   | 否   | —                 |
| `status`       | `VARCHAR(16)`              | 否   | 否   | —                 |
| `moderated_by` | `UUID`                     | 是   | 否   | —                 |
| `moderated_at` | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`           | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`      | `BIGINT`                   | 否   | 否   | —                 |
| `deleted_at`   | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_comments_body_not_empty`：`length(body) > 0`
- CHECK `ck_comments_parent_not_self`：`parent_id IS NULL OR parent_id <> id`
- CHECK `ck_comments_request_hash_shape`：`request_hash ~ '^[0-9a-f]{64}$'`
- CHECK `ck_comments_status_valid`：`status IN ('pending', 'approved', 'rejected', 'hidden')`
- FK `fk_comments_author_id_users`：`(author_id) → (users.id)`；ON DELETE CASCADE
- FK `fk_comments_moderated_by_users`：`(moderated_by) → (users.id)`；ON DELETE SET NULL
- FK `fk_comments_parent_id_comments`：`(parent_id) → (comments.id)`；ON DELETE CASCADE
- FK `fk_comments_parent_id_resource_id`：`(parent_id, resource_id) → (comments.id, comments.resource_id)`；ON DELETE CASCADE
- FK `fk_comments_resource_id_resources`：`(resource_id) → (resources.id)`；ON DELETE CASCADE
- UNIQUE `uq_comments_author_client`：`(author_id, client_id)`
- UNIQUE `uq_comments_id_resource_id`：`(id, resource_id)`

### 显式索引

- `ix_comments_author_id_created_at`：`(comments.author_id, comments.created_at)`
- `ix_comments_parent_id`：`(comments.parent_id)`
- `ix_comments_resource_id_status_created_at`：`(comments.resource_id, comments.status, comments.created_at, comments.id)`

## conversation_summaries

模型：[源文件](../../backend/src/autumn_backend/db/models/knowledge.py)，类定义位于第 286 行。

| 列                 | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------ | -------------------------- | ---- | ---- | ----------------- |
| `conversation_id`  | `UUID`                     | 否   | 否   | —                 |
| `upto_message_seq` | `BIGINT`                   | 否   | 否   | —                 |
| `body_text`        | `TEXT`                     | 否   | 否   | —                 |
| `scope_epoch`      | `BIGINT`                   | 否   | 否   | —                 |
| `status`           | `VARCHAR(16)`              | 否   | 否   | —                 |
| `id`               | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`          | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_conversation_summaries_body_text_not_empty`：`length(body_text) > 0`
- CHECK `ck_conversation_summaries_scope_epoch_non_negative`：`scope_epoch >= 0`
- CHECK `ck_conversation_summaries_status_valid`：`status IN ('active', 'stale')`
- CHECK `ck_conversation_summaries_upto_message_seq_positive`：`upto_message_seq >= 1`
- FK `fk_conversation_summaries_conversation_id_conversations`：`(conversation_id) → (conversations.id)`；ON DELETE CASCADE

### 显式索引

- `ix_conversation_summaries_conversation_id`：`(conversation_summaries.conversation_id, conversation_summaries.upto_message_seq)`
- `uq_conversation_summaries_active`：UNIQUE `(conversation_summaries.conversation_id)`；谓词 `status = 'active'`

## conversations

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 84 行。

| 列                    | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| --------------------- | -------------------------- | ---- | ---- | ----------------- |
| `user_id`             | `UUID`                     | 否   | 否   | —                 |
| `mode`                | `VARCHAR(16)`              | 否   | 否   | —                 |
| `title`               | `TEXT`                     | 否   | 否   | —                 |
| `next_message_seq`    | `BIGINT`                   | 否   | 否   | —                 |
| `retention_policy_id` | `UUID`                     | 是   | 否   | —                 |
| `expires_at`          | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`                  | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`             | `BIGINT`                   | 否   | 否   | —                 |
| `deleted_at`          | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_conversations_mode_valid`：`mode IN ('public', 'owner')`
- CHECK `ck_conversations_next_message_seq_positive`：`next_message_seq >= 1`
- FK `fk_conversations_retention_policy_id_retention_policies`：`(retention_policy_id) → (retention_policies.id)`；ON DELETE SET NULL
- FK `fk_conversations_user_id_users`：`(user_id) → (users.id)`；ON DELETE CASCADE
- UNIQUE `uq_conversations_id_user_id`：`(id, user_id)`

### 显式索引

- `ix_conversations_active_user_id`：`(conversations.user_id)`；谓词 `deleted_at IS NULL`
- `ix_conversations_user_id_updated_at`：`(conversations.user_id, conversations.updated_at, conversations.id)`

## file_objects

模型：[源文件](../../backend/src/autumn_backend/db/models/content.py)，类定义位于第 218 行。

| 列                | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ----------------- | -------------------------- | ---- | ---- | ----------------- |
| `object_key`      | `VARCHAR(512)`             | 否   | 否   | —                 |
| `status`          | `VARCHAR(16)`              | 否   | 否   | —                 |
| `sha256`          | `TEXT`                     | 否   | 否   | —                 |
| `media_type`      | `TEXT`                     | 否   | 否   | —                 |
| `byte_size`       | `BIGINT`                   | 否   | 否   | —                 |
| `storage_backend` | `VARCHAR(32)`              | 否   | 否   | —                 |
| `owner_id`        | `UUID`                     | 否   | 否   | —                 |
| `resource_id`     | `UUID`                     | 是   | 否   | —                 |
| `error_code`      | `VARCHAR(64)`              | 是   | 否   | —                 |
| `finalized_at`    | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`              | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`      | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`      | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`         | `BIGINT`                   | 否   | 否   | —                 |
| `deleted_at`      | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_file_objects_byte_size_non_negative`：`byte_size >= 0`
- CHECK `ck_file_objects_finalized_at_matches_status`：`(status = 'ready') = (finalized_at IS NOT NULL)`
- CHECK `ck_file_objects_object_key_not_empty`：`length(object_key) > 0`
- CHECK `ck_file_objects_sha256_not_empty`：`length(sha256) > 0`
- CHECK `ck_file_objects_status_valid`：`status IN ('staged', 'ready', 'pending_delete', 'failed')`
- FK `fk_file_objects_owner_id_users`：`(owner_id) → (users.id)`；ON DELETE CASCADE
- FK `fk_file_objects_resource_id_resources`：`(resource_id) → (resources.id)`；ON DELETE SET NULL
- UNIQUE `uq_file_objects_object_key`：`(object_key)`

### 显式索引

- `ix_file_objects_resource_id`：`(file_objects.resource_id)`
- `ix_file_objects_status_created_at`：`(file_objects.status, file_objects.created_at)`

## jobs

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 651 行。

| 列                 | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------ | -------------------------- | ---- | ---- | ----------------- |
| `kind`             | `TEXT`                     | 否   | 否   | —                 |
| `status`           | `VARCHAR(16)`              | 否   | 否   | —                 |
| `phase`            | `VARCHAR(16)`              | 是   | 否   | —                 |
| `actor_id`         | `UUID`                     | 是   | 否   | —                 |
| `auth_session_id`  | `UUID`                     | 是   | 否   | —                 |
| `run_id`           | `UUID`                     | 是   | 否   | —                 |
| `resource_id`      | `UUID`                     | 是   | 否   | —                 |
| `payload`          | `JSONB`                    | 是   | 否   | —                 |
| `idempotency_key`  | `TEXT`                     | 否   | 否   | —                 |
| `attempts`         | `INTEGER`                  | 否   | 否   | —                 |
| `max_attempts`     | `INTEGER`                  | 否   | 否   | —                 |
| `available_at`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `lease_token`      | `UUID`                     | 是   | 否   | —                 |
| `lease_expires_at` | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `heartbeat_at`     | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `progress`         | `INTEGER`                  | 是   | 否   | —                 |
| `result`           | `JSONB`                    | 是   | 否   | —                 |
| `error_code`       | `VARCHAR(64)`              | 是   | 否   | —                 |
| `id`               | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`          | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_jobs_attempts_non_negative`：`attempts >= 0`
- CHECK `ck_jobs_idempotency_key_not_empty`：`length(idempotency_key) > 0`
- CHECK `ck_jobs_kind_not_empty`：`length(kind) > 0`
- CHECK `ck_jobs_kind_shape`：`kind ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'`
- CHECK `ck_jobs_lease_fields_match_status`：`(status = 'running') = (lease_token IS NOT NULL) AND (status = 'running') = (lease_expires_at IS NOT NULL)`
- CHECK `ck_jobs_max_attempts_positive`：`max_attempts >= 1`
- CHECK `ck_jobs_payload_is_object`：`payload IS NULL OR jsonb_typeof(payload) = 'object'`
- CHECK `ck_jobs_phase_valid`：`phase IN ('fetching', 'parsing', 'embedding', 'indexing', 'finalizing', 'deleting')`
- CHECK `ck_jobs_progress_in_range`：`progress IS NULL OR (progress >= 0 AND progress <= 100)`
- CHECK `ck_jobs_result_is_object`：`result IS NULL OR jsonb_typeof(result) = 'object'`
- CHECK `ck_jobs_status_valid`：`status IN ('queued', 'running', 'waiting_auth', 'succeeded', 'failed', 'cancelling', 'cancelled')`
- FK `fk_jobs_actor_id_users`：`(actor_id) → (users.id)`；ON DELETE CASCADE
- FK `fk_jobs_auth_session_id_auth_sessions`：`(auth_session_id) → (auth_sessions.id)`；ON DELETE SET NULL
- FK `fk_jobs_resource_id_resources`：`(resource_id) → (resources.id)`；ON DELETE CASCADE
- FK `fk_jobs_run_id_runs`：`(run_id) → (runs.id)`；ON DELETE CASCADE
- UNIQUE `uq_jobs_idempotency_key`：`(idempotency_key)`

### 显式索引

- `ix_jobs_lease_expires_at`：`(jobs.lease_expires_at)`
- `ix_jobs_resource_id`：`(jobs.resource_id)`
- `ix_jobs_run_id`：`(jobs.run_id)`
- `ix_jobs_status_available_at`：`(jobs.status, jobs.available_at)`

## knowledge_chunks

模型：[源文件](../../backend/src/autumn_backend/db/models/knowledge.py)，类定义位于第 168 行。

| 列             | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| -------------- | -------------------------- | ---- | ---- | ----------------- |
| `index_id`     | `UUID`                     | 否   | 否   | —                 |
| `chunk_no`     | `INTEGER`                  | 否   | 否   | —                 |
| `content_text` | `TEXT`                     | 否   | 否   | —                 |
| `locator`      | `JSONB`                    | 是   | 否   | —                 |
| `token_count`  | `INTEGER`                  | 是   | 否   | —                 |
| `embedding`    | `VECTOR(1024)`             | 否   | 否   | —                 |
| `id`           | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `created_at`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_knowledge_chunks_chunk_no_non_negative`：`chunk_no >= 0`
- CHECK `ck_knowledge_chunks_content_text_not_empty`：`length(content_text) > 0`
- CHECK `ck_knowledge_chunks_locator_is_object`：`locator IS NULL OR jsonb_typeof(locator) = 'object'`
- CHECK `ck_knowledge_chunks_token_count_non_negative`：`token_count IS NULL OR token_count >= 0`
- FK `fk_knowledge_chunks_index_id_knowledge_indexes`：`(index_id) → (knowledge_indexes.id)`；ON DELETE CASCADE
- UNIQUE `uq_knowledge_chunks_index_chunk_no`：`(index_id, chunk_no)`

### 显式索引

无额外显式索引；主键和 UNIQUE 仍会建立相应索引。

## knowledge_indexes

模型：[源文件](../../backend/src/autumn_backend/db/models/knowledge.py)，类定义位于第 71 行。

| 列                    | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| --------------------- | -------------------------- | ---- | ---- | ----------------- |
| `resource_id`         | `UUID`                     | 否   | 否   | —                 |
| `revision_id`         | `UUID`                     | 否   | 否   | —                 |
| `publication_id`      | `UUID`                     | 是   | 否   | —                 |
| `scope`               | `VARCHAR(16)`              | 否   | 否   | —                 |
| `embedding_provider`  | `TEXT`                     | 否   | 否   | —                 |
| `embedding_model`     | `TEXT`                     | 否   | 否   | —                 |
| `embedding_dimension` | `INTEGER`                  | 否   | 否   | —                 |
| `generation`          | `INTEGER`                  | 否   | 否   | —                 |
| `status`              | `VARCHAR(16)`              | 否   | 否   | —                 |
| `is_active`           | `BOOLEAN`                  | 否   | 否   | —                 |
| `content_hash`        | `TEXT`                     | 否   | 否   | —                 |
| `error_code`          | `VARCHAR(64)`              | 是   | 否   | —                 |
| `id`                  | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`             | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_knowledge_indexes_active_must_be_ready`：`NOT is_active OR status = 'ready'`
- CHECK `ck_knowledge_indexes_content_hash_not_empty`：`length(content_hash) > 0`
- CHECK `ck_knowledge_indexes_embedding_dimension_matches_column`：`embedding_dimension = 1024`
- CHECK `ck_knowledge_indexes_embedding_model_not_empty`：`length(embedding_model) > 0`
- CHECK `ck_knowledge_indexes_embedding_provider_not_empty`：`length(embedding_provider) > 0`
- CHECK `ck_knowledge_indexes_generation_positive`：`generation >= 1`
- CHECK `ck_knowledge_indexes_publication_id_matches_scope`：`(scope = 'public') = (publication_id IS NOT NULL)`
- CHECK `ck_knowledge_indexes_scope_valid`：`scope IN ('owner', 'public')`
- CHECK `ck_knowledge_indexes_status_valid`：`status IN ('queued', 'building', 'ready', 'failed', 'retired')`
- FK `fk_knowledge_indexes_resource_id_resources`：`(resource_id) → (resources.id)`；ON DELETE CASCADE
- FK `fk_knowledge_indexes_resource_id_revision_id`：`(resource_id, revision_id) → (resource_versions.resource_id, resource_versions.id)`；ON DELETE CASCADE
- FK `fk_knowledge_indexes_resource_revision_publication`：`(resource_id, revision_id, publication_id) → (publications.resource_id, publications.revision_id, publications.id)`；ON DELETE CASCADE
- UNIQUE `uq_knowledge_indexes_id_embedding_dimension`：`(id, embedding_dimension)`

### 显式索引

- `ix_knowledge_indexes_resource_id_scope`：`(knowledge_indexes.resource_id, knowledge_indexes.scope)`
- `ix_knowledge_indexes_revision_id`：`(knowledge_indexes.revision_id)`
- `uq_knowledge_indexes_owner_active`：UNIQUE `(knowledge_indexes.revision_id)`；谓词 `is_active AND scope = 'owner'`
- `uq_knowledge_indexes_public_active`：UNIQUE `(knowledge_indexes.publication_id)`；谓词 `is_active AND scope = 'public'`

## memories

模型：[源文件](../../backend/src/autumn_backend/db/models/knowledge.py)，类定义位于第 324 行。

| 列                  | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------- | -------------------------- | ---- | ---- | ----------------- |
| `user_id`           | `UUID`                     | 否   | 否   | —                 |
| `kind`              | `VARCHAR(16)`              | 否   | 否   | —                 |
| `content_text`      | `TEXT`                     | 否   | 否   | —                 |
| `origin_run_id`     | `UUID`                     | 是   | 否   | —                 |
| `origin_message_id` | `UUID`                     | 是   | 否   | —                 |
| `confirmed_at`      | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `expires_at`        | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`                | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`        | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`        | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`           | `BIGINT`                   | 否   | 否   | —                 |
| `deleted_at`        | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_memories_content_text_not_empty`：`length(content_text) > 0`
- CHECK `ck_memories_kind_valid`：`kind IN ('preference', 'fact')`
- FK `fk_memories_origin_message_id_messages`：`(origin_message_id) → (messages.id)`；ON DELETE SET NULL
- FK `fk_memories_origin_run_id_runs`：`(origin_run_id) → (runs.id)`；ON DELETE SET NULL

### 显式索引

- `ix_memories_active_user_id`：`(memories.user_id)`；谓词 `deleted_at IS NULL`
- `ix_memories_origin_run_id`：`(memories.origin_run_id)`
- `ix_memories_user_id_created_at`：`(memories.user_id, memories.created_at)`

## messages

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 256 行。

| 列                  | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------- | -------------------------- | ---- | ---- | ----------------- |
| `conversation_id`   | `UUID`                     | 否   | 否   | —                 |
| `run_id`            | `UUID`                     | 是   | 否   | —                 |
| `seq`               | `BIGINT`                   | 否   | 否   | —                 |
| `role`              | `VARCHAR(16)`              | 否   | 否   | —                 |
| `body_text`         | `TEXT`                     | 否   | 否   | —                 |
| `content_version`   | `BIGINT`                   | 否   | 否   | —                 |
| `status`            | `VARCHAR(16)`              | 否   | 否   | —                 |
| `client_message_id` | `UUID`                     | 是   | 否   | —                 |
| `id`                | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`        | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`        | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`           | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_messages_content_version_positive`：`content_version >= 1`
- CHECK `ck_messages_role_valid`：`role IN ('user', 'assistant')`
- CHECK `ck_messages_seq_positive`：`seq >= 1`
- CHECK `ck_messages_status_valid`：`status IN ('composing', 'complete', 'interrupted', 'hidden')`
- FK `fk_messages_conversation_id_conversations`：`(conversation_id) → (conversations.id)`；ON DELETE CASCADE
- FK `fk_messages_run_id_conversation_id`：`(run_id, conversation_id) → (runs.id, runs.conversation_id)`；ON DELETE NO ACTION
- UNIQUE `uq_messages_conversation_seq`：`(conversation_id, seq)`
- UNIQUE `uq_messages_id_conversation_id`：`(id, conversation_id)`

### 显式索引

- `ix_messages_conversation_id_seq`：`(messages.conversation_id, messages.seq)`
- `uq_messages_conversation_id_client_message_id`：UNIQUE `(messages.conversation_id, messages.client_message_id)`；谓词 `client_message_id IS NOT NULL`

## provider_calls

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 554 行。

| 列                    | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| --------------------- | -------------------------- | ---- | ---- | ----------------- |
| `run_id`              | `UUID`                     | 是   | 否   | —                 |
| `job_id`              | `UUID`                     | 是   | 否   | —                 |
| `provider`            | `TEXT`                     | 否   | 否   | —                 |
| `model`               | `TEXT`                     | 是   | 否   | —                 |
| `purpose`             | `VARCHAR(16)`              | 否   | 否   | —                 |
| `logical_call_key`    | `TEXT`                     | 否   | 否   | —                 |
| `attempt_no`          | `INTEGER`                  | 否   | 否   | —                 |
| `status`              | `VARCHAR(16)`              | 否   | 否   | —                 |
| `external_request_id` | `TEXT`                     | 是   | 否   | —                 |
| `input_tokens`        | `BIGINT`                   | 是   | 否   | —                 |
| `output_tokens`       | `BIGINT`                   | 是   | 否   | —                 |
| `search_units`        | `BIGINT`                   | 是   | 否   | —                 |
| `estimated_cost`      | `NUMERIC(20, 8)`           | 是   | 否   | —                 |
| `actual_cost`         | `NUMERIC(20, 8)`           | 是   | 否   | —                 |
| `currency`            | `VARCHAR(3)`               | 是   | 否   | —                 |
| `started_at`          | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `finished_at`         | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `error_code`          | `VARCHAR(64)`              | 是   | 否   | —                 |
| `id`                  | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`             | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_provider_calls_actual_cost_non_negative`：`actual_cost IS NULL OR actual_cost >= 0`
- CHECK `ck_provider_calls_attempt_no_positive`：`attempt_no >= 1`
- CHECK `ck_provider_calls_currency_required_with_cost`：`(estimated_cost IS NULL AND actual_cost IS NULL) OR currency IS NOT NULL`
- CHECK `ck_provider_calls_currency_shape`：`currency IS NULL OR length(currency) = 3`
- CHECK `ck_provider_calls_estimated_cost_non_negative`：`estimated_cost IS NULL OR estimated_cost >= 0`
- CHECK `ck_provider_calls_finished_at_matches_terminal_status`：`status NOT IN ('succeeded', 'failed', 'unknown') OR finished_at IS NOT NULL`
- CHECK `ck_provider_calls_input_tokens_non_negative`：`input_tokens IS NULL OR input_tokens >= 0`
- CHECK `ck_provider_calls_logical_call_key_not_empty`：`length(logical_call_key) > 0`
- CHECK `ck_provider_calls_output_tokens_non_negative`：`output_tokens IS NULL OR output_tokens >= 0`
- CHECK `ck_provider_calls_prepared_not_started`：`status <> 'prepared' OR started_at IS NULL`
- CHECK `ck_provider_calls_purpose_valid`：`purpose IN ('chat', 'tool', 'embedding', 'rerank', 'search', 'fetch', 'email')`
- CHECK `ck_provider_calls_search_units_non_negative`：`search_units IS NULL OR search_units >= 0`
- CHECK `ck_provider_calls_status_valid`：`status IN ('prepared', 'dispatched', 'succeeded', 'failed', 'unknown')`
- CHECK `ck_provider_calls_traceable_target`：`run_id IS NOT NULL OR job_id IS NOT NULL`
- FK `fk_provider_calls_job_id_jobs`：`(job_id) → (jobs.id)`；ON DELETE SET NULL
- FK `fk_provider_calls_run_id_runs`：`(run_id) → (runs.id)`；ON DELETE SET NULL
- UNIQUE `uq_provider_calls_logical_key_attempt`：`(logical_call_key, attempt_no)`

### 显式索引

- `ix_provider_calls_job_id`：`(provider_calls.job_id)`
- `ix_provider_calls_provider_created_at`：`(provider_calls.provider, provider_calls.created_at)`
- `ix_provider_calls_run_id`：`(provider_calls.run_id)`
- `ix_provider_calls_status_created_at`：`(provider_calls.status, provider_calls.created_at)`

## publications

模型：[源文件](../../backend/src/autumn_backend/db/models/content.py)，类定义位于第 341 行。

| 列                     | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ---------------------- | -------------------------- | ---- | ---- | ----------------- |
| `resource_id`          | `UUID`                     | 否   | 否   | —                 |
| `revision_id`          | `UUID`                     | 否   | 否   | —                 |
| `publication_no`       | `INTEGER`                  | 否   | 否   | —                 |
| `public_title`         | `TEXT`                     | 是   | 否   | —                 |
| `public_body`          | `TEXT`                     | 是   | 否   | —                 |
| `public_note`          | `TEXT`                     | 是   | 否   | —                 |
| `public_url`           | `TEXT`                     | 是   | 否   | —                 |
| `public_tags`          | `TEXT[]`                   | 否   | 否   | '{}'::text[]      |
| `public_fields`        | `TEXT[]`                   | 否   | 否   | '{}'::text[]      |
| `ai_enabled`           | `BOOLEAN`                  | 否   | 否   | —                 |
| `raw_download_enabled` | `BOOLEAN`                  | 否   | 否   | —                 |
| `published_by`         | `UUID`                     | 否   | 否   | —                 |
| `published_at`         | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `revoked_at`           | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`                   | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`           | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`           | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`              | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_publications_public_body_matches_fields`：`('body' = ANY(public_fields)) = (public_body IS NOT NULL)`
- CHECK `ck_publications_public_fields_whitelisted`：`public_fields <@ ARRAY['title', 'body', 'note', 'url', 'tags']::text[]`
- CHECK `ck_publications_public_note_matches_fields`：`('note' = ANY(public_fields)) = (public_note IS NOT NULL)`
- CHECK `ck_publications_public_tags_matches_fields`：`('tags' = ANY(public_fields)) = (cardinality(public_tags) > 0)`
- CHECK `ck_publications_public_title_matches_fields`：`('title' = ANY(public_fields)) = (public_title IS NOT NULL)`
- CHECK `ck_publications_public_url_matches_fields`：`('url' = ANY(public_fields)) = (public_url IS NOT NULL)`
- CHECK `ck_publications_publication_no_positive`：`publication_no >= 1`
- FK `fk_publications_published_by_users`：`(published_by) → (users.id)`；ON DELETE RESTRICT
- FK `fk_publications_resource_id_resources`：`(resource_id) → (resources.id)`；ON DELETE CASCADE
- FK `fk_publications_resource_id_revision_id`：`(resource_id, revision_id) → (resource_versions.resource_id, resource_versions.id)`；ON DELETE RESTRICT
- UNIQUE `uq_publications_resource_publication`：`(resource_id, publication_no)`
- UNIQUE `uq_publications_resource_revision_id`：`(resource_id, revision_id, id)`

### 显式索引

- `ix_publications_resource_id_published_at`：`(publications.resource_id, publications.published_at)`
- `uq_publications_resource_id_current`：UNIQUE `(publications.resource_id)`；谓词 `revoked_at IS NULL`

## quota_buckets

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 455 行。

| 列               | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ---------------- | -------------------------- | ---- | ---- | ----------------- |
| `user_id`        | `UUID`                     | 否   | 否   | —                 |
| `window_start`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —                 |
| `window_end`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —                 |
| `timezone`       | `TEXT`                     | 否   | 否   | —                 |
| `used`           | `INTEGER`                  | 否   | 否   | —                 |
| `reserved`       | `INTEGER`                  | 否   | 否   | —                 |
| `policy_version` | `BIGINT`                   | 否   | 否   | —                 |
| `id`             | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`        | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_quota_buckets_policy_version_non_negative`：`policy_version >= 0`
- CHECK `ck_quota_buckets_reserved_non_negative`：`reserved >= 0`
- CHECK `ck_quota_buckets_used_non_negative`：`used >= 0`
- CHECK `ck_quota_buckets_window_ordered`：`window_end > window_start`
- FK `fk_quota_buckets_user_id_users`：`(user_id) → (users.id)`；ON DELETE CASCADE
- UNIQUE `uq_quota_buckets_id_user_id`：`(id, user_id)`
- UNIQUE `uq_quota_buckets_user_window`：`(user_id, window_start)`

### 显式索引

- `ix_quota_buckets_window_start`：`(quota_buckets.window_start)`

## quota_reservations

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 493 行。

| 列           | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------ | -------------------------- | ---- | ---- | ----------------- |
| `run_id`     | `UUID`                     | 否   | 否   | —                 |
| `bucket_id`  | `UUID`                     | 否   | 否   | —                 |
| `user_id`    | `UUID`                     | 否   | 否   | —                 |
| `amount`     | `INTEGER`                  | 否   | 否   | —                 |
| `status`     | `VARCHAR(16)`              | 否   | 否   | —                 |
| `charged_at` | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `settled_at` | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`         | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at` | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`    | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_quota_reservations_amount_is_one`：`amount = 1`
- CHECK `ck_quota_reservations_reserved_not_charged`：`status <> 'reserved' OR charged_at IS NULL`
- CHECK `ck_quota_reservations_settled_at_matches_terminal_status`：`status NOT IN ('released', 'refunded') OR settled_at IS NOT NULL`
- CHECK `ck_quota_reservations_status_valid`：`status IN ('reserved', 'charged', 'released', 'refunded')`
- FK `fk_quota_reservations_bucket_id_user_id`：`(bucket_id, user_id) → (quota_buckets.id, quota_buckets.user_id)`；ON DELETE CASCADE
- FK `fk_quota_reservations_run_id_user_id`：`(run_id, user_id) → (runs.id, runs.user_id)`；ON DELETE CASCADE
- UNIQUE `uq_quota_reservations_run_id`：`(run_id)`

### 显式索引

- `ix_quota_reservations_bucket_id_status`：`(quota_reservations.bucket_id, quota_reservations.status)`
- `ix_quota_reservations_user_id_created_at`：`(quota_reservations.user_id, quota_reservations.created_at)`

## rate_limit_buckets

模型：[源文件](../../backend/src/autumn_backend/db/models/identity.py)，类定义位于第 227 行。

| 列             | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值 |
| -------------- | -------------------------- | ---- | ---- | ------------ |
| `scope_hash`   | `TEXT`                     | 否   | 是   | —            |
| `policy_key`   | `TEXT`                     | 否   | 是   | —            |
| `window_start` | `TIMESTAMP WITH TIME ZONE` | 否   | 是   | —            |
| `window_end`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —            |
| `hits`         | `BIGINT`                   | 否   | 否   | —            |
| `expires_at`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | —            |
| `updated_at`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()        |
| `created_at`   | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()        |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_rate_limit_buckets_hits_non_negative`：`hits >= 0`
- CHECK `ck_rate_limit_buckets_policy_key_not_empty`：`length(policy_key) > 0`
- CHECK `ck_rate_limit_buckets_scope_hash_not_empty`：`length(scope_hash) > 0`
- CHECK `ck_rate_limit_buckets_window_ordered`：`window_end > window_start`

### 显式索引

- `ix_rate_limit_buckets_expires_at`：`(rate_limit_buckets.expires_at)`

## reports

模型：[源文件](../../backend/src/autumn_backend/db/models/content.py)，类定义位于第 508 行。

| 列                | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ----------------- | -------------------------- | ---- | ---- | ----------------- |
| `reporter_id`     | `UUID`                     | 否   | 否   | —                 |
| `comment_id`      | `UUID`                     | 否   | 否   | —                 |
| `reason`          | `TEXT`                     | 否   | 否   | —                 |
| `status`          | `VARCHAR(16)`              | 否   | 否   | —                 |
| `handled_by`      | `UUID`                     | 是   | 否   | —                 |
| `resolution_note` | `TEXT`                     | 是   | 否   | —                 |
| `resolved_at`     | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`              | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`      | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`      | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`         | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_reports_reason_not_empty`：`length(reason) > 0`
- CHECK `ck_reports_resolved_at_matches_status`：`(status = 'open') = (resolved_at IS NULL)`
- CHECK `ck_reports_status_valid`：`status IN ('open', 'resolved', 'dismissed')`
- FK `fk_reports_comment_id_comments`：`(comment_id) → (comments.id)`；ON DELETE CASCADE
- FK `fk_reports_handled_by_users`：`(handled_by) → (users.id)`；ON DELETE SET NULL
- FK `fk_reports_reporter_id_users`：`(reporter_id) → (users.id)`；ON DELETE CASCADE

### 显式索引

- `ix_reports_comment_id`：`(reports.comment_id)`
- `ix_reports_status_created_at`：`(reports.status, reports.created_at)`
- `uq_reports_reporter_id_comment_id_open`：UNIQUE `(reports.reporter_id, reports.comment_id)`；谓词 `status = 'open'`

## resource_versions

模型：[源文件](../../backend/src/autumn_backend/db/models/content.py)，类定义位于第 266 行。

| 列                 | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------ | -------------------------- | ---- | ---- | ----------------- |
| `resource_id`      | `UUID`                     | 否   | 否   | —                 |
| `revision_no`      | `INTEGER`                  | 否   | 否   | —                 |
| `created_by`       | `UUID`                     | 是   | 否   | —                 |
| `title`            | `TEXT`                     | 是   | 否   | —                 |
| `body_text`        | `TEXT`                     | 是   | 否   | —                 |
| `content_format`   | `VARCHAR(16)`              | 否   | 否   | —                 |
| `url`              | `TEXT`                     | 是   | 否   | —                 |
| `private_note`     | `TEXT`                     | 是   | 否   | —                 |
| `tags`             | `TEXT[]`                   | 否   | 否   | '{}'::text[]      |
| `linked_source_id` | `UUID`                     | 是   | 否   | —                 |
| `source_metadata`  | `JSONB`                    | 是   | 否   | —                 |
| `file_object_key`  | `VARCHAR(512)`             | 是   | 否   | —                 |
| `file_sha256`      | `TEXT`                     | 是   | 否   | —                 |
| `media_type`       | `TEXT`                     | 是   | 否   | —                 |
| `byte_size`        | `BIGINT`                   | 是   | 否   | —                 |
| `id`               | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`          | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_resource_versions_byte_size_non_negative`：`byte_size IS NULL OR byte_size >= 0`
- CHECK `ck_resource_versions_content_format_valid`：`content_format IN ('markdown', 'plain')`
- CHECK `ck_resource_versions_file_metadata_consistent`：`(file_object_key IS NULL) = (file_sha256 IS NULL) AND (file_object_key IS NULL) = (media_type IS NULL) AND (file_object_key IS NULL) = (byte_size IS NULL)`
- CHECK `ck_resource_versions_revision_no_positive`：`revision_no >= 1`
- CHECK `ck_resource_versions_source_metadata_is_object`：`source_metadata IS NULL OR jsonb_typeof(source_metadata) = 'object'`
- FK `fk_resource_versions_created_by_users`：`(created_by) → (users.id)`；ON DELETE SET NULL
- FK `fk_resource_versions_file_object_key_file_objects`：`(file_object_key) → (file_objects.object_key)`；ON DELETE RESTRICT
- FK `fk_resource_versions_linked_source_id_resources`：`(linked_source_id) → (resources.id)`；ON DELETE SET NULL
- FK `fk_resource_versions_resource_id_resources`：`(resource_id) → (resources.id)`；ON DELETE CASCADE
- UNIQUE `uq_resource_versions_resource_id_id`：`(resource_id, id)`
- UNIQUE `uq_resource_versions_resource_revision`：`(resource_id, revision_no)`

### 显式索引

- `ix_resource_versions_resource_id_created_at`：`(resource_versions.resource_id, resource_versions.created_at)`

## resources

模型：[源文件](../../backend/src/autumn_backend/db/models/content.py)，类定义位于第 147 行。

| 列                    | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| --------------------- | -------------------------- | ---- | ---- | ----------------- |
| `owner_id`            | `UUID`                     | 否   | 否   | —                 |
| `kind`                | `VARCHAR(16)`              | 否   | 否   | —                 |
| `slug`                | `VARCHAR(160)`             | 否   | 否   | —                 |
| `current_revision_id` | `UUID`                     | 是   | 否   | —                 |
| `acl_version`         | `BIGINT`                   | 否   | 否   | —                 |
| `retention_policy_id` | `UUID`                     | 是   | 否   | —                 |
| `expires_at`          | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `id`                  | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`          | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`             | `BIGINT`                   | 否   | 否   | —                 |
| `archived_at`         | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `deleted_at`          | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_resources_acl_version_non_negative`：`acl_version >= 0`
- CHECK `ck_resources_kind_valid`：`kind IN ('article', 'bookmark', 'document', 'webpage')`
- CHECK `ck_resources_slug_not_empty`：`length(slug) > 0`
- FK `fk_resources_current_revision_id_resource_versions`：`(current_revision_id, id) → (resource_versions.id, resource_versions.resource_id)`；ON DELETE NO ACTION；DEFERRABLE DEFERRED
- FK `fk_resources_owner_id_users`：`(owner_id) → (users.id)`；ON DELETE CASCADE
- FK `fk_resources_retention_policy_id_retention_policies`：`(retention_policy_id) → (retention_policies.id)`；ON DELETE SET NULL
- UNIQUE `uq_resources_id_owner_id`：`(id, owner_id)`
- UNIQUE `uq_resources_slug`：`(slug)`

### 显式索引

- `ix_resources_active_owner_id`：`(resources.owner_id)`；谓词 `deleted_at IS NULL AND archived_at IS NULL`
- `ix_resources_expires_at`：`(resources.expires_at)`；谓词 `expires_at IS NOT NULL`
- `ix_resources_owner_id_kind_created_at`：`(resources.owner_id, resources.kind, resources.created_at)`

## retention_policies

模型：[源文件](../../backend/src/autumn_backend/db/models/content.py)，类定义位于第 73 行。

| 列                 | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------ | -------------------------- | ---- | ---- | ----------------- |
| `scope`            | `VARCHAR(32)`              | 否   | 否   | —                 |
| `resource_kind`    | `VARCHAR(16)`              | 是   | 否   | —                 |
| `mode`             | `VARCHAR(16)`              | 否   | 否   | —                 |
| `ttl_days`         | `INTEGER`                  | 是   | 否   | —                 |
| `anchor`           | `VARCHAR(16)`              | 否   | 否   | —                 |
| `applies_from`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `include_existing` | `BOOLEAN`                  | 否   | 否   | —                 |
| `is_active`        | `BOOLEAN`                  | 否   | 否   | —                 |
| `created_by`       | `UUID`                     | 否   | 否   | —                 |
| `id`               | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`       | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`          | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_retention_policies_anchor_valid`：`anchor IN ('created_at', 'updated_at')`
- CHECK `ck_retention_policies_mode_valid`：`mode IN ('forever', 'ttl')`
- CHECK `ck_retention_policies_resource_kind_valid`：`resource_kind IN ('article', 'bookmark', 'document', 'webpage')`
- CHECK `ck_retention_policies_scope_valid`：`scope IN ('resources', 'conversations', 'audit_events', 'runtime_logs')`
- CHECK `ck_retention_policies_ttl_matches_mode`：`(mode = 'forever' AND ttl_days IS NULL) OR (mode = 'ttl' AND ttl_days IS NOT NULL AND ttl_days > 0)`
- FK `fk_retention_policies_created_by_users`：`(created_by) → (users.id)`；ON DELETE RESTRICT

### 显式索引

- `uq_retention_policies_active_global`：UNIQUE `(retention_policies.scope)`；谓词 `is_active AND resource_kind IS NULL`
- `uq_retention_policies_active_kind`：UNIQUE `(retention_policies.scope, retention_policies.resource_kind)`；谓词 `is_active AND resource_kind IS NOT NULL`

## run_events

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 319 行。

| 列           | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值 |
| ------------ | -------------------------- | ---- | ---- | ------------ |
| `run_id`     | `UUID`                     | 否   | 是   | —            |
| `seq`        | `BIGINT`                   | 否   | 是   | —            |
| `type`       | `VARCHAR(32)`              | 否   | 否   | —            |
| `payload`    | `JSONB`                    | 是   | 否   | —            |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()        |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_run_events_payload_is_object`：`payload IS NULL OR jsonb_typeof(payload) = 'object'`
- CHECK `ck_run_events_seq_non_negative`：`seq >= 0`
- CHECK `ck_run_events_type_valid`：`type IN ('run.status', 'message.snapshot', 'tool.started', 'tool.finished', 'knowledge.processing', 'action.proposed', 'action.succeeded', 'source.invalidated', 'scope.changed', 'error', 'done')`
- FK `fk_run_events_run_id_runs`：`(run_id) → (runs.id)`；ON DELETE CASCADE

### 显式索引

- `ix_run_events_run_id_created_at`：`(run_events.run_id, run_events.created_at)`

## run_sources

模型：[源文件](../../backend/src/autumn_backend/db/models/knowledge.py)，类定义位于第 203 行。

| 列                     | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ---------------------- | -------------------------- | ---- | ---- | ----------------- |
| `run_id`               | `UUID`                     | 否   | 否   | —                 |
| `source_type`          | `VARCHAR(16)`              | 否   | 否   | —                 |
| `context_generation`   | `BIGINT`                   | 否   | 否   | 1                 |
| `source_key`           | `TEXT`                     | 否   | 否   | —                 |
| `resource_id`          | `UUID`                     | 是   | 否   | —                 |
| `revision_id`          | `UUID`                     | 是   | 否   | —                 |
| `publication_id`       | `UUID`                     | 是   | 否   | —                 |
| `index_id`             | `UUID`                     | 是   | 否   | —                 |
| `chunk_id`             | `UUID`                     | 是   | 否   | —                 |
| `observed_acl_version` | `BIGINT`                   | 否   | 否   | —                 |
| `locator`              | `JSONB`                    | 是   | 否   | —                 |
| `web_url`              | `TEXT`                     | 是   | 否   | —                 |
| `web_title`            | `TEXT`                     | 是   | 否   | —                 |
| `fetched_at`           | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `excerpt`              | `TEXT`                     | 是   | 否   | —                 |
| `id`                   | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `created_at`           | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_run_sources_context_generation_positive`：`context_generation >= 1`
- CHECK `ck_run_sources_index_chunk_paired`：`(index_id IS NULL) = (chunk_id IS NULL)`
- CHECK `ck_run_sources_locator_is_object`：`locator IS NULL OR jsonb_typeof(locator) = 'object'`
- CHECK `ck_run_sources_observed_acl_version_non_negative`：`observed_acl_version >= 0`
- CHECK `ck_run_sources_source_key_not_empty`：`length(source_key) > 0`
- CHECK `ck_run_sources_source_type_consistent`：`(source_type = 'resource' AND resource_id IS NOT NULL AND revision_id IS NOT NULL AND web_url IS NULL AND web_title IS NULL) OR (source_type = 'web' AND resource_id IS NULL AND revision_id IS NULL AND publication_id IS NULL AND index_id IS NULL AND chunk_id IS NULL AND web_url IS NOT NULL)`
- CHECK `ck_run_sources_source_type_valid`：`source_type IN ('resource', 'web')`
- FK `fk_run_sources_chunk_id_knowledge_chunks`：`(chunk_id) → (knowledge_chunks.id)`；ON DELETE SET NULL
- FK `fk_run_sources_index_id_knowledge_indexes`：`(index_id) → (knowledge_indexes.id)`；ON DELETE SET NULL
- FK `fk_run_sources_publication_id_publications`：`(publication_id) → (publications.id)`；ON DELETE SET NULL
- FK `fk_run_sources_resource_id_revision_id`：`(resource_id, revision_id) → (resource_versions.resource_id, resource_versions.id)`；ON DELETE SET NULL
- FK `fk_run_sources_run_id_runs`：`(run_id) → (runs.id)`；ON DELETE CASCADE
- UNIQUE `uq_run_sources_run_id_source_key`：`(run_id, source_key)`

### 显式索引

- `ix_run_sources_resource_id`：`(run_sources.resource_id)`
- `ix_run_sources_run_id`：`(run_sources.run_id)`

## runs

模型：[源文件](../../backend/src/autumn_backend/db/models/runtime.py)，类定义位于第 124 行。

| 列                     | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ---------------------- | -------------------------- | ---- | ---- | ----------------- |
| `user_id`              | `UUID`                     | 否   | 否   | —                 |
| `conversation_id`      | `UUID`                     | 否   | 否   | —                 |
| `idempotency_key`      | `VARCHAR(128)`             | 否   | 否   | —                 |
| `request_hash`         | `TEXT`                     | 否   | 否   | —                 |
| `input_message_id`     | `UUID`                     | 是   | 否   | —                 |
| `current_message_id`   | `UUID`                     | 是   | 否   | —                 |
| `auth_session_id`      | `UUID`                     | 是   | 否   | —                 |
| `status`               | `VARCHAR(16)`              | 否   | 否   | —                 |
| `scope_epoch`          | `BIGINT`                   | 否   | 否   | —                 |
| `checkpoint_thread_id` | `TEXT`                     | 否   | 否   | —                 |
| `next_event_seq`       | `BIGINT`                   | 否   | 否   | —                 |
| `execution_generation` | `BIGINT`                   | 否   | 否   | 1                 |
| `config_snapshot`      | `JSONB`                    | 否   | 否   | '{}'::jsonb       |
| `input_request`        | `JSONB`                    | 是   | 否   | —                 |
| `started_at`           | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `finished_at`          | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `error_code`           | `VARCHAR(64)`              | 是   | 否   | —                 |
| `id`                   | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`           | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`           | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`              | `BIGINT`                   | 否   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_runs_checkpoint_thread_id_not_empty`：`length(checkpoint_thread_id) > 0`
- CHECK `ck_runs_config_snapshot_is_object`：`config_snapshot IS NULL OR jsonb_typeof(config_snapshot) = 'object'`
- CHECK `ck_runs_execution_generation_positive`：`execution_generation >= 1`
- CHECK `ck_runs_finished_at_matches_terminal_status`：`(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)`
- CHECK `ck_runs_input_request_is_object`：`input_request IS NULL OR jsonb_typeof(input_request) = 'object'`
- CHECK `ck_runs_next_event_seq_positive`：`next_event_seq >= 1`
- CHECK `ck_runs_request_hash_not_empty`：`length(request_hash) > 0`
- CHECK `ck_runs_scope_epoch_non_negative`：`scope_epoch >= 0`
- CHECK `ck_runs_status_valid`：`status IN ('queued', 'running', 'waiting_input', 'waiting_approval', 'waiting_auth', 'succeeded', 'failed', 'cancelling', 'cancelled')`
- FK `fk_runs_auth_session_id_user_id`：`(auth_session_id, user_id) → (auth_sessions.id, auth_sessions.user_id)`；ON DELETE NO ACTION
- FK `fk_runs_conversation_id_user_id_conversations`：`(conversation_id, user_id) → (conversations.id, conversations.user_id)`；ON DELETE CASCADE
- FK `fk_runs_current_message_id_messages`：`(current_message_id, conversation_id) → (messages.id, messages.conversation_id)`；ON DELETE SET NULL；DEFERRABLE DEFERRED
- FK `fk_runs_input_message_id_messages`：`(input_message_id, conversation_id) → (messages.id, messages.conversation_id)`；ON DELETE SET NULL；DEFERRABLE DEFERRED
- UNIQUE `uq_runs_id_conversation_id`：`(id, conversation_id)`
- UNIQUE `uq_runs_id_user_id`：`(id, user_id)`
- UNIQUE `uq_runs_user_id_idempotency_key`：`(user_id, idempotency_key)`

### 显式索引

- `ix_runs_conversation_id_created_at`：`(runs.conversation_id, runs.created_at)`
- `ix_runs_status_created_at`：`(runs.status, runs.created_at)`
- `ix_runs_user_id_status`：`(runs.user_id, runs.status)`
- `uq_runs_conversation_id_non_terminal`：UNIQUE `(runs.conversation_id)`；谓词 `status IN ('queued', 'running', 'waiting_input', 'waiting_approval', 'waiting_auth', 'cancelling')`

## settings

模型：[源文件](../../backend/src/autumn_backend/db/models/identity.py)，类定义位于第 260 行。

| 列               | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值 |
| ---------------- | -------------------------- | ---- | ---- | ------------ |
| `key`            | `TEXT`                     | 否   | 是   | —            |
| `value`          | `JSONB`                    | 否   | 否   | —            |
| `schema_version` | `BIGINT`                   | 否   | 否   | —            |
| `updated_by`     | `UUID`                     | 是   | 否   | —            |
| `version`        | `BIGINT`                   | 否   | 否   | —            |
| `updated_at`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()        |
| `created_at`     | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()        |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_settings_key_lowercase`：`key = lower(key)`
- CHECK `ck_settings_key_not_empty`：`length(key) > 0`
- CHECK `ck_settings_schema_version_positive`：`schema_version >= 1`
- CHECK `ck_settings_version_non_negative`：`version >= 0`
- FK `fk_settings_updated_by_users`：`(updated_by) → (users.id)`；ON DELETE SET NULL

### 显式索引

无额外显式索引；主键和 UNIQUE 仍会建立相应索引。

## users

模型：[源文件](../../backend/src/autumn_backend/db/models/identity.py)，类定义位于第 53 行。

| 列                  | PostgreSQL 类型            | 可空 | 主键 | 数据库默认值      |
| ------------------- | -------------------------- | ---- | ---- | ----------------- |
| `email_normalized`  | `TEXT`                     | 否   | 否   | —                 |
| `display_name`      | `VARCHAR(80)`              | 是   | 否   | —                 |
| `password_hash`     | `TEXT`                     | 否   | 否   | —                 |
| `role`              | `VARCHAR(16)`              | 否   | 否   | —                 |
| `status`            | `VARCHAR(32)`              | 否   | 否   | —                 |
| `verified_at`       | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `ai_cooldown_until` | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |
| `auth_version`      | `BIGINT`                   | 否   | 否   | —                 |
| `id`                | `UUID`                     | 否   | 是   | gen_random_uuid() |
| `updated_at`        | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `created_at`        | `TIMESTAMP WITH TIME ZONE` | 否   | 否   | now()             |
| `version`           | `BIGINT`                   | 否   | 否   | —                 |
| `deleted_at`        | `TIMESTAMP WITH TIME ZONE` | 是   | 否   | —                 |

Python 侧默认值不在本表中冒充数据库默认值。

### 约束

- CHECK `ck_users_auth_version_positive`：`auth_version >= 1`
- CHECK `ck_users_email_normalized_canonical`：`email_normalized = lower(btrim(email_normalized))`
- CHECK `ck_users_email_normalized_not_empty`：`length(email_normalized) > 0`
- CHECK `ck_users_role_valid`：`role IN ('member', 'owner')`
- CHECK `ck_users_status_valid`：`status IN ('pending_verification', 'active', 'disabled')`
- UNIQUE `uq_users_email_normalized`：`(email_normalized)`

### 显式索引

- `ix_users_status_created_at`：`(users.status, users.created_at)`

## 迁移文件顺序

共 7 个业务迁移文件；最新 revision 为 `a7c19e23b806`。

- [20261005_1928_8585dcac6197_identity_foundation.py](../../backend/alembic/versions/20261005_1928_8585dcac6197_identity_foundation.py)
- [20261005_1928_f91f72539039_content_files_comments_reports.py](../../backend/alembic/versions/20261005_1928_f91f72539039_content_files_comments_reports.py)
- [20261005_1932_2c485916b4d2_run_quota_job_audit.py](../../backend/alembic/versions/20261005_1932_2c485916b4d2_run_quota_job_audit.py)
- [20261005_1932_c8dc294b55a0_knowledge_sources_summaries_memories.py](../../backend/alembic/versions/20261005_1932_c8dc294b55a0_knowledge_sources_summaries_memories.py)
- [20261005_2200_b8a71e06d204_comment_request_identity.py](../../backend/alembic/versions/20261005_2200_b8a71e06d204_comment_request_identity.py)
- [20261006_2300_d31e82a9f647_run_execution_generation.py](../../backend/alembic/versions/20261006_2300_d31e82a9f647_run_execution_generation.py)
- [20261006_2350_a7c19e23b806_content_action_types.py](../../backend/alembic/versions/20261006_2350_a7c19e23b806_content_action_types.py)
