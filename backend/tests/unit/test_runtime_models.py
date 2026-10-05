"""对话、运行、配额、作业与审计模型：元数据契约单测（不需要数据库）。

对应 ``docs/architecture/database.md`` §7、§8、§9 与 ``db/models/runtime.py``。
真库验收在 ``tests/integration/test_runtime_constraints.py``。

这一批是约束最密集的一批：幂等身份、状态机、lease 一致性、复合归属外键与
"循环外键可延迟"都在这里落地。
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Table,
    UniqueConstraint,
)

from autumn_backend.db import models
from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    NON_TERMINAL_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    ActionAuthorizationKind,
    ActionStatus,
    ActionType,
    AuditResult,
    ConversationMode,
    JobPhase,
    JobStatus,
    MessageRole,
    MessageStatus,
    ProviderCallPurpose,
    ProviderCallStatus,
    QuotaReservationStatus,
    RunEventType,
    RunStatus,
    enum_check_expression,
    in_predicate,
)
from autumn_backend.db.mixins import timestamped_tables

pytestmark = pytest.mark.unit

A6_TABLES = (
    "conversations",
    "messages",
    "runs",
    "run_events",
    "actions",
    "quota_buckets",
    "quota_reservations",
    "jobs",
    "provider_calls",
    "audit_events",
)

#: 只追加表：只有 created_at，没有 updated_at，也没有乐观锁版本。
APPEND_ONLY_TABLES = ("run_events", "audit_events")


@pytest.fixture(scope="module", autouse=True)
def _registered_models() -> None:
    models.load_all_models()


def table(name: str) -> Table:
    return Base.metadata.tables[name]


def checks(name: str) -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in table(name).constraints
        if isinstance(c, CheckConstraint) and c.name is not None
    }


def uniques(name: str) -> set[str]:
    return {
        c.name
        for c in table(name).constraints
        if isinstance(c, UniqueConstraint) and c.name is not None
    }


def composite_fk(name: str, fk_name: str) -> ForeignKeyConstraint:
    return next(
        c
        for c in table(name).constraints
        if isinstance(c, ForeignKeyConstraint) and c.name == fk_name
    )


def index_by_name(name: str, index_name: str) -> Index:
    return next(i for i in table(name).indexes if i.name == index_name)


def partial_predicate(name: str, index_name: str) -> str:
    where = index_by_name(name, index_name).dialect_options["postgresql"]["where"]
    assert where is not None, f"{index_name} 必须是部分索引"
    return str(where)


class TestTableRegistration:
    @pytest.mark.parametrize("name", A6_TABLES)
    def test_table_exists(self, name: str) -> None:
        assert name in Base.metadata.tables

    def test_mutable_tables_are_timestamped(self) -> None:
        mutable = tuple(name for name in A6_TABLES if name not in APPEND_ONLY_TABLES)
        assert set(mutable).issubset({t.name for t in timestamped_tables()})

    def test_append_only_tables_have_only_created_at(self) -> None:
        """文档 §1、§7、§9：只追加表没有 updated_at，也不挂时间戳触发器。"""
        for name in APPEND_ONLY_TABLES:
            columns = set(table(name).columns.keys())
            assert "created_at" in columns
            assert "updated_at" not in columns
            assert "version" not in columns
            assert name not in {t.name for t in timestamped_tables()}

    def test_single_ck_prefix(self) -> None:
        for name in A6_TABLES:
            for constraint_name in checks(name):
                assert constraint_name.count("ck_") == 1, constraint_name


class TestConversationAndMessageConstraints:
    def test_mode_check_lists_enum_values(self) -> None:
        assert checks("conversations")["ck_conversations_mode_valid"] == (
            enum_check_expression("mode", ConversationMode)
        )

    def test_identity_pair_for_run_composite_reference(self) -> None:
        assert "uq_conversations_id_user_id" in uniques("conversations")

    def test_seq_allocator_is_positive(self) -> None:
        assert (
            "next_message_seq >= 1"
            in checks("conversations")["ck_conversations_next_message_seq_positive"]
        )

    def test_active_list_index_is_partial(self) -> None:
        predicate = partial_predicate("conversations", "ix_conversations_active_user_id")
        assert "deleted_at IS NULL" in predicate

    def test_message_seq_unique_per_conversation(self) -> None:
        assert "uq_messages_conversation_seq" in uniques("messages")

    def test_message_identity_pair_for_run_pointers(self) -> None:
        """``runs.input_message_id`` / ``current_message_id`` 依赖这个唯一键。"""
        assert "uq_messages_id_conversation_id" in uniques("messages")

    def test_client_message_id_dedupe_is_partial(self) -> None:
        index = index_by_name("messages", "uq_messages_conversation_id_client_message_id")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["conversation_id", "client_message_id"]
        assert "client_message_id IS NOT NULL" in partial_predicate(
            "messages", "uq_messages_conversation_id_client_message_id"
        )

    def test_message_enum_checks(self) -> None:
        expressions = checks("messages")
        assert expressions["ck_messages_role_valid"] == enum_check_expression("role", MessageRole)
        assert expressions["ck_messages_status_valid"] == enum_check_expression(
            "status", MessageStatus
        )
        assert "seq >= 1" in expressions["ck_messages_seq_positive"]
        assert "content_version >= 1" in expressions["ck_messages_content_version_positive"]

    def test_message_holds_full_text_not_protocol_payload(self) -> None:
        """messages 存完整正文；工具协议消息不混入普通对话展示。"""
        columns = set(table("messages").columns.keys())
        assert {"body_text", "content_version", "status"}.issubset(columns)
        assert not {"delta", "tool_name", "tool_payload"} & columns

    def test_run_belongs_to_same_conversation_by_composite_fk(self) -> None:
        fk = composite_fk("messages", "fk_messages_run_id_conversation_id")
        assert [c.name for c in fk.columns] == ["run_id", "conversation_id"]
        assert [e.target_fullname for e in fk.elements] == [
            "runs.id",
            "runs.conversation_id",
        ]


class TestRunConstraints:
    def test_idempotency_identity(self) -> None:
        assert "uq_runs_user_id_idempotency_key" in uniques("runs")

    def test_request_hash_present(self) -> None:
        assert "request_hash" in table("runs").columns
        assert "length(request_hash) > 0" in checks("runs")["ck_runs_request_hash_not_empty"]

    def test_single_non_terminal_run_per_conversation(self) -> None:
        index = index_by_name("runs", "uq_runs_conversation_id_non_terminal")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["conversation_id"]
        # 谓词必须与枚举定义一致：索引与枚举不可能各自漂移。
        assert in_predicate("status", NON_TERMINAL_RUN_STATUSES) in partial_predicate(
            "runs", "uq_runs_conversation_id_non_terminal"
        )

    def test_non_terminal_predicate_covers_every_active_state(self) -> None:
        predicate = in_predicate("status", NON_TERMINAL_RUN_STATUSES)
        assert predicate == (
            "status IN ('queued', 'running', 'waiting_input', 'waiting_approval', "
            "'waiting_auth', 'cancelling')"
        )
        for value in TERMINAL_RUN_STATUSES:
            assert value not in predicate

    def test_status_sets_are_disjoint_and_cover_the_enum(self) -> None:
        active = set(NON_TERMINAL_RUN_STATUSES)
        terminal = set(TERMINAL_RUN_STATUSES)
        assert not active & terminal
        assert active | terminal == {value.value for value in RunStatus}

    def test_status_check_lists_enum_values(self) -> None:
        assert checks("runs")["ck_runs_status_valid"] == enum_check_expression("status", RunStatus)

    def test_next_event_seq_is_the_seq_allocator(self) -> None:
        column = table("runs").c.next_event_seq
        assert column.nullable is False
        assert isinstance(column.type, BigInteger)
        assert "next_event_seq >= 1" in checks("runs")["ck_runs_next_event_seq_positive"]

    def test_finished_at_matches_terminal_status(self) -> None:
        expression = checks("runs")["ck_runs_finished_at_matches_terminal_status"]
        assert in_predicate("status", TERMINAL_RUN_STATUSES) in expression
        assert "finished_at IS NOT NULL" in expression

    def test_composite_ownership_foreign_keys(self) -> None:
        """run 必须与 conversation 同属一个用户；授权会话也必须属于同一用户。"""
        conversation_fk = composite_fk("runs", "fk_runs_conversation_id_user_id_conversations")
        assert [c.name for c in conversation_fk.columns] == ["conversation_id", "user_id"]
        assert [e.target_fullname for e in conversation_fk.elements] == [
            "conversations.id",
            "conversations.user_id",
        ]
        session_fk = composite_fk("runs", "fk_runs_auth_session_id_user_id")
        assert [c.name for c in session_fk.columns] == ["auth_session_id", "user_id"]
        assert [e.target_fullname for e in session_fk.elements] == [
            "auth_sessions.id",
            "auth_sessions.user_id",
        ]

    def test_message_pointers_are_deferrable_cyclic_foreign_keys(self) -> None:
        """文档 §11：循环外键在相关表创建后补充，并明确可延迟检查。"""
        for fk_name in (
            "fk_runs_input_message_id_messages",
            "fk_runs_current_message_id_messages",
        ):
            fk = composite_fk("runs", fk_name)
            assert fk.deferrable is True
            assert fk.initially == "DEFERRED"

    def test_scope_epoch_and_jsonb_guards(self) -> None:
        expressions = checks("runs")
        assert "scope_epoch >= 0" in expressions["ck_runs_scope_epoch_non_negative"]
        assert (
            "length(checkpoint_thread_id) > 0"
            in expressions["ck_runs_checkpoint_thread_id_not_empty"]
        )
        assert (
            "jsonb_typeof(config_snapshot) = 'object'"
            in expressions["ck_runs_config_snapshot_is_object"]
        )
        assert (
            "jsonb_typeof(input_request) = 'object'"
            in expressions["ck_runs_input_request_is_object"]
        )


class TestRunEventConstraints:
    def test_primary_key_is_run_and_seq(self) -> None:
        pk = table("run_events").primary_key
        assert pk.name == "pk_run_events"
        assert [c.name for c in pk.columns] == ["run_id", "seq"]

    def test_event_type_check(self) -> None:
        assert checks("run_events")["ck_run_events_type_valid"] == (
            enum_check_expression("type", RunEventType)
        )

    def test_seq_non_negative(self) -> None:
        assert "seq >= 0" in checks("run_events")["ck_run_events_seq_non_negative"]

    def test_no_external_seq_column(self) -> None:
        """seq 只能由 ``runs.next_event_seq`` 分配；不存在"外部给定序号"的列。"""
        columns = set(table("run_events").columns.keys())
        assert "seq" in columns
        assert not {"external_seq", "client_seq"} & columns

    def test_payload_is_metadata_only(self) -> None:
        """payload 只含状态与对象 ID，不重复保存消息全文。"""
        columns = set(table("run_events").columns.keys())
        assert {"payload", "type", "created_at"}.issubset(columns)
        assert not {"body_text", "content", "prompt"} & columns


class TestQuotaConstraints:
    def test_bucket_window_unique(self) -> None:
        assert "uq_quota_buckets_user_window" in uniques("quota_buckets")

    def test_bucket_identity_pair_for_reservations(self) -> None:
        assert "uq_quota_buckets_id_user_id" in uniques("quota_buckets")

    def test_bucket_counters_and_window(self) -> None:
        expressions = checks("quota_buckets")
        assert "used >= 0" in expressions["ck_quota_buckets_used_non_negative"]
        assert "reserved >= 0" in expressions["ck_quota_buckets_reserved_non_negative"]
        assert "policy_version >= 0" in expressions["ck_quota_buckets_policy_version_non_negative"]
        assert "window_end > window_start" in expressions["ck_quota_buckets_window_ordered"]

    def test_no_static_limit_column(self) -> None:
        """限额从当前受控配置读取：桶里不保存静态限额，否则站长无法降额。"""
        columns = set(table("quota_buckets").columns.keys())
        assert "policy_version" in columns
        assert not {"limit_value", "daily_limit"} & columns

    def test_bucket_window_is_utc_with_explicit_timezone(self) -> None:
        for name in ("window_start", "window_end"):
            assert table("quota_buckets").c[name].type.timezone is True
        assert "timezone" in table("quota_buckets").columns

    def test_reservation_run_id_is_unique(self) -> None:
        assert "uq_quota_reservations_run_id" in uniques("quota_reservations")

    def test_reservation_amount_is_one(self) -> None:
        assert checks("quota_reservations")["ck_quota_reservations_amount_is_one"] == "amount = 1"

    def test_reservation_status_check(self) -> None:
        assert checks("quota_reservations")["ck_quota_reservations_status_valid"] == (
            enum_check_expression("status", QuotaReservationStatus)
        )

    def test_reservation_state_machine_guards(self) -> None:
        expressions = checks("quota_reservations")
        assert expressions["ck_quota_reservations_reserved_not_charged"] == (
            "status <> 'reserved' OR charged_at IS NULL"
        )
        assert (
            "status NOT IN ('released', 'refunded')"
            in expressions["ck_quota_reservations_settled_at_matches_terminal_status"]
        )
        assert (
            "settled_at IS NOT NULL"
            in expressions["ck_quota_reservations_settled_at_matches_terminal_status"]
        )
        columns = set(table("quota_reservations").columns.keys())
        assert {"charged_at", "settled_at"}.issubset(columns)

    def test_composite_user_ownership_foreign_keys(self) -> None:
        """run 用户与桶用户都必须与本预留用户一致——由结构而不是服务约定保证。"""
        run_fk = composite_fk("quota_reservations", "fk_quota_reservations_run_id_user_id")
        assert [c.name for c in run_fk.columns] == ["run_id", "user_id"]
        assert [e.target_fullname for e in run_fk.elements] == ["runs.id", "runs.user_id"]
        bucket_fk = composite_fk("quota_reservations", "fk_quota_reservations_bucket_id_user_id")
        assert [c.name for c in bucket_fk.columns] == ["bucket_id", "user_id"]
        assert [e.target_fullname for e in bucket_fk.elements] == [
            "quota_buckets.id",
            "quota_buckets.user_id",
        ]


class TestJobConstraints:
    def test_status_and_phase_checks(self) -> None:
        expressions = checks("jobs")
        assert expressions["ck_jobs_status_valid"] == enum_check_expression("status", JobStatus)
        assert expressions["ck_jobs_phase_valid"] == enum_check_expression("phase", JobPhase)

    def test_kind_shape_is_domain_verb(self) -> None:
        assert checks("jobs")["ck_jobs_kind_shape"] == (
            "kind ~ '^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$'"
        )

    def test_idempotency_key_is_unique(self) -> None:
        """排队去重由 ``UNIQUE(idempotency_key)`` 承担（文档 §9）。"""
        assert "uq_jobs_idempotency_key" in uniques("jobs")
        assert "uq_jobs_type_dedupe_active" not in {index.name for index in table("jobs").indexes}

    def test_lease_fields_match_status(self) -> None:
        expression = checks("jobs")["ck_jobs_lease_fields_match_status"]
        assert "(status = 'running') = (lease_token IS NOT NULL)" in expression
        assert "(status = 'running') = (lease_expires_at IS NOT NULL)" in expression

    def test_progress_in_range(self) -> None:
        expression = checks("jobs")["ck_jobs_progress_in_range"]
        assert "progress >= 0" in expression
        assert "progress <= 100" in expression
        # 不确定进度写 NULL，不编造百分比。
        assert "progress IS NULL" in expression

    def test_attempts_and_max_attempts_guards(self) -> None:
        expressions = checks("jobs")
        assert "attempts >= 0" in expressions["ck_jobs_attempts_non_negative"]
        assert "max_attempts >= 1" in expressions["ck_jobs_max_attempts_positive"]

    def test_claim_and_lease_indexes_exist(self) -> None:
        """claim 的 WHERE status='queued' AND available_at<=now() ORDER BY available_at。"""
        claim = index_by_name("jobs", "ix_jobs_status_available_at")
        assert [c.name for c in claim.columns] == ["status", "available_at"]
        assert index_by_name("jobs", "ix_jobs_lease_expires_at") is not None
        assert table("jobs").c.available_at.server_default is not None

    def test_job_carries_identity_without_cookie(self) -> None:
        columns = set(table("jobs").columns.keys())
        assert {"actor_id", "auth_session_id", "run_id", "resource_id"}.issubset(columns)
        assert "cookie" not in columns


class TestProviderCallConstraints:
    def test_logical_call_identity_is_unique_per_attempt(self) -> None:
        """评审 D10：稳定逻辑身份与物理尝试编号分开。"""
        assert "uq_provider_calls_logical_key_attempt" in uniques("provider_calls")
        assert "uq_provider_calls_job_purpose_attempt" not in uniques("provider_calls")
        assert (
            "attempt_no >= 1" in checks("provider_calls")["ck_provider_calls_attempt_no_positive"]
        )

    def test_status_and_purpose_checks(self) -> None:
        expressions = checks("provider_calls")
        assert expressions["ck_provider_calls_status_valid"] == enum_check_expression(
            "status", ProviderCallStatus
        )
        assert expressions["ck_provider_calls_purpose_valid"] == enum_check_expression(
            "purpose", ProviderCallPurpose
        )

    def test_prepared_has_no_start_time(self) -> None:
        assert checks("provider_calls")["ck_provider_calls_prepared_not_started"] == (
            "status <> 'prepared' OR started_at IS NULL"
        )

    def test_finished_at_matches_terminal_states(self) -> None:
        expression = checks("provider_calls")[
            "ck_provider_calls_finished_at_matches_terminal_status"
        ]
        assert "finished_at IS NOT NULL" in expression
        for value in ("succeeded", "failed", "unknown"):
            assert f"'{value}'" in expression

    def test_unknown_is_a_first_class_state(self) -> None:
        """unknown 不能被当成"失败且零成本"：它是独立状态且有 finished_at。"""
        assert (
            ProviderCallStatus.UNKNOWN.value
            in checks("provider_calls")["ck_provider_calls_status_valid"]
        )
        assert (
            "unknown"
            in checks("provider_calls")["ck_provider_calls_finished_at_matches_terminal_status"]
        )

    def test_traceable_target(self) -> None:
        assert checks("provider_calls")["ck_provider_calls_traceable_target"] == (
            "run_id IS NOT NULL OR job_id IS NOT NULL"
        )

    def test_cost_requires_currency(self) -> None:
        expressions = checks("provider_calls")
        assert (
            "currency IS NOT NULL" in expressions["ck_provider_calls_currency_required_with_cost"]
        )
        assert "length(currency) = 3" in expressions["ck_provider_calls_currency_shape"]
        assert "estimated_cost >= 0" in expressions["ck_provider_calls_estimated_cost_non_negative"]
        assert "actual_cost >= 0" in expressions["ck_provider_calls_actual_cost_non_negative"]

    def test_stale_job_fk_is_set_null_not_cascade(self) -> None:
        """保留成本记录：job 被清理不应连带删除调用记录。"""
        assert next(iter(table("provider_calls").c.job_id.foreign_keys)).ondelete == "SET NULL"
        assert next(iter(table("provider_calls").c.run_id.foreign_keys)).ondelete == "SET NULL"


class TestAuditEventConstraints:
    def test_append_only_shape(self) -> None:
        columns = set(table("audit_events").columns.keys())
        assert "created_at" in columns
        assert not {"updated_at", "version", "deleted_at", "status"} & columns

    def test_result_check(self) -> None:
        assert checks("audit_events")["ck_audit_events_result_valid"] == (
            enum_check_expression("result", AuditResult)
        )

    def test_event_type_shape_is_not_frozen(self) -> None:
        """审计类型会演进：只约束 domain.verb 形状，不冻结取值集合。"""
        expression = checks("audit_events")["ck_audit_events_event_type_shape"]
        assert expression == "event_type ~ '^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$'"
        assert "ANY" not in expression

    def test_version_fields_are_non_negative(self) -> None:
        expressions = checks("audit_events")
        assert "before_version >= 0" in expressions["ck_audit_events_before_version_non_negative"]
        assert "after_version >= 0" in expressions["ck_audit_events_after_version_non_negative"]

    def test_actor_and_resource_detach_on_delete(self) -> None:
        for column in ("actor_id", "resource_id", "action_id"):
            assert next(iter(table("audit_events").c[column].foreign_keys)).ondelete == "SET NULL"

    def test_lookup_indexes(self) -> None:
        names = {i.name for i in table("audit_events").indexes}
        assert "ix_audit_events_resource_id_created_at" in names
        assert "ix_audit_events_actor_id_created_at" in names
        assert "ix_audit_events_event_type_created_at" in names


class TestActionConstraints:
    def test_idempotency_identity_is_not_null_actor_and_key(self) -> None:
        """幂等身份必须是不可空列，否则 NULL 会让唯一约束失效。"""
        assert "uq_actions_actor_id_idempotency_key" in uniques("actions")
        unique = next(
            c
            for c in table("actions").constraints
            if isinstance(c, UniqueConstraint) and c.name == "uq_actions_actor_id_idempotency_key"
        )
        assert [c.name for c in unique.columns] == ["actor_id", "idempotency_key"]
        for column_name in ("actor_id", "idempotency_key"):
            assert table("actions").c[column_name].nullable is False

    def test_target_is_optional_but_not_part_of_uniqueness(self) -> None:
        """设置类动作没有目标对象；它可空，但不参与唯一性。"""
        assert table("actions").c.target_resource_id.nullable is True
        unique = next(
            c
            for c in table("actions").constraints
            if isinstance(c, UniqueConstraint) and c.name == "uq_actions_actor_id_idempotency_key"
        )
        assert "target_resource_id" not in {c.name for c in unique.columns}

    def test_type_status_and_authorization_checks(self) -> None:
        expressions = checks("actions")
        assert expressions["ck_actions_type_valid"] == enum_check_expression("type", ActionType)
        assert expressions["ck_actions_status_valid"] == enum_check_expression(
            "status", ActionStatus
        )
        assert expressions["ck_actions_authorization_kind_valid"] == enum_check_expression(
            "authorization_kind", ActionAuthorizationKind
        )

    def test_both_expected_versions_are_persisted(self) -> None:
        """内容版本与 ACL 版本分开保存、分别比较。"""
        columns = set(table("actions").columns.keys())
        assert {"expected_version", "expected_acl_version"}.issubset(columns)
        assert "target_version" not in columns
        expressions = checks("actions")
        assert expressions["ck_actions_expected_version_non_negative"] == (
            "expected_version IS NULL OR expected_version >= 0"
        )
        assert expressions["ck_actions_expected_acl_version_non_negative"] == (
            "expected_acl_version IS NULL OR expected_acl_version >= 0"
        )

    def test_status_timestamp_consistency(self) -> None:
        expressions = checks("actions")
        assert (
            expressions["ck_actions_confirmed_statuses_require_confirmed_at"]
            == "status NOT IN ('ready', 'running', 'succeeded', 'failed') "
            "OR confirmed_at IS NOT NULL"
        )
        assert (
            expressions["ck_actions_unconfirmed_has_no_confirmed_at"]
            == "status NOT IN ('proposed', 'awaiting_confirmation') OR confirmed_at IS NULL"
        )
        assert expressions["ck_actions_executed_at_matches_status"] == (
            "(status = 'succeeded') = (executed_at IS NOT NULL)"
        )
        assert "expires_at > created_at" in expressions["ck_actions_expires_after_created"]

    def test_expiry_lookup_index(self) -> None:
        index = index_by_name("actions", "ix_actions_status_expires_at")
        assert [c.name for c in index.columns] == ["status", "expires_at"]


class TestInPredicateHelper:
    def test_produces_literal_in_predicate(self) -> None:
        assert in_predicate("status", ["a", "b"]) == "status IN ('a', 'b')"

    def test_rejects_empty_input(self) -> None:
        with pytest.raises(ValueError, match="至少要有一个取值"):
            in_predicate("status", [])
