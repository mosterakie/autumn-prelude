"""A6 第三批模型：元数据契约单测（不需要数据库）。

本批是约束最密集的一批；实施顺序表里点名的每一条都有对应断言。
真库验收在 ``tests/integration/test_runtime_constraints.py``。
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, PrimaryKeyConstraint, Table, UniqueConstraint

from autumn_backend.db import models
from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    NON_TERMINAL_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    ActionKind,
    ActionStatus,
    ConversationMode,
    EventType,
    JobStatus,
    MessageRole,
    ModelProfile,
    ProviderCallPurpose,
    ProviderCallStatus,
    QuotaReservationStatus,
    RunStatus,
    in_predicate,
)
from autumn_backend.db.mixins import timestamped_tables

pytestmark = pytest.mark.unit

A6_TABLES = (
    "conversations",
    "messages",
    "runs",
    "run_events",
    "quota_buckets",
    "quota_reservations",
    "jobs",
    "provider_calls",
    "audit_events",
    "actions",
)


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


def index_by_name(name: str, index_name: str):
    return next(i for i in table(name).indexes if i.name == index_name)


class TestTableRegistration:
    @pytest.mark.parametrize("name", A6_TABLES)
    def test_table_exists(self, name: str) -> None:
        assert name in Base.metadata.tables

    def test_all_tables_are_timestamped(self) -> None:
        assert set(A6_TABLES).issubset({t.name for t in timestamped_tables()})

    def test_single_ck_prefix(self) -> None:
        for name in A6_TABLES:
            for constraint_name in checks(name):
                assert constraint_name.count("ck_") == 1, constraint_name

    def test_run_sources_fk_to_runs_is_declared(self) -> None:
        """A5 时 runs 还不存在；A6 补上外键。"""
        fk = next(iter(table("run_sources").c.run_id.foreign_keys))
        assert fk.target_fullname == "runs.id"
        assert fk.ondelete == "CASCADE"

    def test_run_events_primary_key_is_run_and_seq(self) -> None:
        pk = next(c for c in table("run_events").constraints if isinstance(c, PrimaryKeyConstraint))
        assert pk.name == "pk_run_events"
        assert [c.name for c in pk.columns] == ["run_id", "seq"]


class TestConversationAndMessageConstraints:
    def test_thread_id_is_unique_and_server_owned(self) -> None:
        assert "uq_conversations_thread_id" in uniques("conversations")

    def test_mode_check_lists_enum_values(self) -> None:
        expression = checks("conversations")["ck_conversations_mode_valid"]
        for value in ConversationMode:
            assert f"'{value.value}'" in expression

    def test_model_profile_check(self) -> None:
        expression = checks("conversations")["ck_conversations_model_profile_valid"]
        for value in ModelProfile:
            assert f"'{value.value}'" in expression

    def test_message_seq_unique_per_conversation(self) -> None:
        assert "uq_messages_conversation_seq" in uniques("messages")

    def test_message_role_check(self) -> None:
        expression = checks("messages")["ck_messages_role_valid"]
        for value in MessageRole:
            assert f"'{value.value}'" in expression

    def test_tool_name_matches_role(self) -> None:
        expression = checks("messages")["ck_messages_tool_name_matches_role"]
        assert "(role = 'tool') = (tool_name IS NOT NULL)" in expression

    def test_message_holds_full_text_and_snapshot_version(self) -> None:
        """messages 存完整文本；run_events 只存事件与元数据。"""
        columns = set(table("messages").columns.keys())
        assert "content" in columns
        assert "content_version" in columns
        assert "delta" not in columns


class TestRunConstraints:
    def test_idempotency_identity(self) -> None:
        assert "uq_runs_user_id_idempotency_key" in uniques("runs")

    def test_request_hash_present(self) -> None:
        assert "request_hash" in table("runs").columns

    def test_single_non_terminal_run_per_conversation(self) -> None:
        index = index_by_name("runs", "uq_runs_conversation_id_non_terminal")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["conversation_id"]
        where = index.dialect_options["postgresql"]["where"]
        assert where is not None
        # 谓词必须与枚举定义一致：索引与枚举不可能各自漂移。
        assert in_predicate("status", NON_TERMINAL_RUN_STATUSES) in str(where)

    def test_non_terminal_predicate_uses_only_the_three_active_states(self) -> None:
        predicate = in_predicate("status", NON_TERMINAL_RUN_STATUSES)
        assert predicate == "status IN ('pending', 'running', 'waiting_input')"
        for value in TERMINAL_RUN_STATUSES:
            assert value not in predicate

    def test_status_sets_are_disjoint_and_cover_the_enum(self) -> None:
        active = set(NON_TERMINAL_RUN_STATUSES)
        terminal = set(TERMINAL_RUN_STATUSES)
        assert not active & terminal
        assert active | terminal == {value.value for value in RunStatus}

    def test_status_check_lists_enum_values(self) -> None:
        expression = checks("runs")["ck_runs_status_valid"]
        for value in RunStatus:
            assert f"'{value.value}'" in expression

    def test_next_event_seq_is_the_seq_allocator(self) -> None:
        column = table("runs").c.next_event_seq
        assert column.nullable is False
        assert "next_event_seq >= 1" in checks("runs")["ck_runs_next_event_seq_positive"]

    def test_finished_at_matches_terminal_status(self) -> None:
        expression = checks("runs")["ck_runs_finished_at_matches_terminal_status"]
        assert "finished_at IS NOT NULL" in expression
        for value in TERMINAL_RUN_STATUSES:
            assert f"'{value}'" in expression

    def test_budget_counters_cannot_be_negative(self) -> None:
        expressions = checks("runs")
        assert "steps_used >= 0" in expressions["ck_runs_steps_used_non_negative"]
        assert "token_used >= 0" in expressions["ck_runs_token_used_non_negative"]
        assert "cost_micro_usd >= 0" in expressions["ck_runs_cost_micro_usd_non_negative"]

    def test_quota_bucket_is_pinned_at_accept_time(self) -> None:
        """run 固定归属受理时的 bucket：跨午夜恢复不换桶、不重扣。"""
        column = table("runs").c.quota_bucket_id
        assert column.nullable is True  # 历史数据或未受理完成的 run
        fk = next(iter(column.foreign_keys))
        assert fk.target_fullname == "quota_buckets.id"
        assert fk.ondelete == "SET NULL"

    def test_terminal_reason_fields_exist_instead_of_more_statuses(self) -> None:
        columns = set(table("runs").columns.keys())
        assert {"error_code", "terminal_reason"}.issubset(columns)
        # 状态集合保持有限：不出现 per-reason 的状态列。
        assert len(list(RunStatus)) == 6

    def test_acl_epoch_snapshot_columns(self) -> None:
        columns = set(table("runs").columns.keys())
        assert {"acl_epoch_at_start", "context_generation"}.issubset(columns)
        assert checks("runs")["ck_runs_context_generation_positive"] == "context_generation >= 1"


class TestRunEventConstraints:
    def test_event_type_check(self) -> None:
        expression = checks("run_events")["ck_run_events_event_type_valid"]
        for value in EventType:
            assert f"'{value.value}'" in expression

    def test_seq_non_negative(self) -> None:
        assert "seq >= 0" in checks("run_events")["ck_run_events_seq_non_negative"]

    def test_no_external_seq_column(self) -> None:
        """seq 只能由 runs.next_event_seq 分配；不存在"外部给定序号"的列。"""
        columns = set(table("run_events").columns.keys())
        assert "seq" in columns
        assert "external_seq" not in columns
        assert "client_seq" not in columns

    def test_object_reference_is_optional_and_versioned(self) -> None:
        columns = set(table("run_events").columns.keys())
        assert {"object_id", "object_version", "context_generation"}.issubset(columns)


class TestQuotaConstraints:
    def test_bucket_window_unique(self) -> None:
        assert "uq_quota_buckets_user_window" in uniques("quota_buckets")

    def test_bucket_counters_non_negative(self) -> None:
        expressions = checks("quota_buckets")
        assert "used >= 0" in expressions["ck_quota_buckets_used_non_negative"]
        assert "reserved >= 0" in expressions["ck_quota_buckets_reserved_non_negative"]
        assert "limit_value >= 0" in expressions["ck_quota_buckets_limit_value_non_negative"]

    def test_bucket_has_usage_within_limit_guard(self) -> None:
        assert (
            checks("quota_buckets")["ck_quota_buckets_usage_within_limit"]
            == "used + reserved <= limit_value"
        )

    def test_bucket_window_is_utc_with_explicit_timezone(self) -> None:
        assert table("quota_buckets").c.window_start.type.timezone is True
        assert "window_timezone" in table("quota_buckets").columns

    def test_reservation_run_id_is_the_idempotency_key(self) -> None:
        assert "uq_quota_reservations_run_id" in uniques("quota_reservations")

    def test_reservation_amount_is_one(self) -> None:
        assert checks("quota_reservations")["ck_quota_reservations_amount_is_one"] == "amount = 1"

    def test_reservation_status_check(self) -> None:
        expression = checks("quota_reservations")["ck_quota_reservations_status_valid"]
        for value in QuotaReservationStatus:
            assert f"'{value.value}'" in expression

    def test_reservation_has_one_timestamp_per_transition(self) -> None:
        columns = set(table("quota_reservations").columns.keys())
        assert {"charged_at", "released_at", "refunded_at"}.issubset(columns)

    def test_reserved_cannot_already_be_charged(self) -> None:
        assert (
            checks("quota_reservations")["ck_quota_reservations_reserved_not_charged"]
            == "status <> 'reserved' OR charged_at IS NULL"
        )


class TestJobConstraints:
    def test_status_check(self) -> None:
        expression = checks("jobs")["ck_jobs_status_valid"]
        for value in JobStatus:
            assert f"'{value.value}'" in expression

    def test_lease_fields_match_status(self) -> None:
        expression = checks("jobs")["ck_jobs_lease_fields_match_status"]
        assert "(status = 'running') = (lease_token IS NOT NULL)" in expression
        assert "(status = 'running') = (lease_expires_at IS NOT NULL)" in expression

    def test_finished_at_matches_terminal_status(self) -> None:
        expression = checks("jobs")["ck_jobs_finished_at_matches_terminal_status"]
        assert "finished_at IS NOT NULL" in expression

    def test_attempts_and_priority_guards(self) -> None:
        expressions = checks("jobs")
        assert "attempts >= 0" in expressions["ck_jobs_attempts_non_negative"]
        assert "max_attempts >= 1" in expressions["ck_jobs_max_attempts_positive"]
        assert "priority >= 0" in expressions["ck_jobs_priority_non_negative"]

    def test_claim_order_index_exists(self) -> None:
        """claim 的 WHERE status='queued' AND available_at<=now() ORDER BY available_at, id。"""
        index = index_by_name("jobs", "ix_jobs_status_available_at")
        assert [c.name for c in index.columns] == ["status", "available_at"]

    def test_lease_expiry_index_exists_for_reclaim(self) -> None:
        assert index_by_name("jobs", "ix_jobs_lease_expires_at") is not None

    def test_active_dedupe_is_partial_unique(self) -> None:
        index = index_by_name("jobs", "uq_jobs_type_dedupe_active")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["job_type", "dedupe_key"]
        where = str(index.dialect_options["postgresql"]["where"])
        assert "queued" in where
        assert "running" in where

    def test_available_at_has_server_default(self) -> None:
        assert table("jobs").c.available_at.server_default is not None

    def test_job_carries_run_id_for_identity_rebuild(self) -> None:
        """worker 从 job 元数据与服务端记录重建身份：不带浏览器 Cookie 明文。"""
        columns = set(table("jobs").columns.keys())
        assert "run_id" in columns
        assert "user_id" in columns
        assert "cookie" not in columns


class TestProviderCallConstraints:
    def test_attempt_identity_is_unique(self) -> None:
        assert "uq_provider_calls_job_purpose_attempt" in uniques("provider_calls")

    def test_status_check(self) -> None:
        expression = checks("provider_calls")["ck_provider_calls_status_valid"]
        for value in ProviderCallStatus:
            assert f"'{value.value}'" in expression

    def test_purpose_check(self) -> None:
        expression = checks("provider_calls")["ck_provider_calls_purpose_valid"]
        for value in ProviderCallPurpose:
            assert f"'{value.value}'" in expression

    def test_prepared_has_no_dispatch_time(self) -> None:
        assert (
            checks("provider_calls")["ck_provider_calls_prepared_not_dispatched"]
            == "status <> 'prepared' OR dispatched_at IS NULL"
        )

    def test_settled_at_matches_terminal_states(self) -> None:
        expression = checks("provider_calls")["ck_provider_calls_settled_at_matches_status"]
        assert "settled_at IS NOT NULL" in expression
        for value in ("succeeded", "failed", "unknown"):
            assert f"'{value}'" in expression

    def test_unknown_is_a_first_class_state(self) -> None:
        """unknown 不能被当成"失败且零成本"：它是独立状态且有 settled_at。"""
        assert (
            ProviderCallStatus.UNKNOWN.value
            in checks("provider_calls")["ck_provider_calls_status_valid"]
        )
        assert "unknown" in checks("provider_calls")["ck_provider_calls_settled_at_matches_status"]

    def test_traceable_target(self) -> None:
        assert (
            checks("provider_calls")["ck_provider_calls_traceable_target"]
            == "job_id IS NOT NULL OR run_id IS NOT NULL"
        )

    def test_cost_and_latency_guards(self) -> None:
        expressions = checks("provider_calls")
        assert (
            "latency_ms IS NULL OR latency_ms >= 0"
            in expressions["ck_provider_calls_latency_non_negative"]
        )
        assert (
            "cost_micro_usd IS NULL OR cost_micro_usd >= 0"
            in expressions["ck_provider_calls_cost_non_negative"]
        )

    def test_has_reconciliation_columns(self) -> None:
        columns = set(table("provider_calls").columns.keys())
        assert {"provider_call_id", "idempotency_key", "reconciled_at"}.issubset(columns)

    def test_stale_job_fk_is_set_null_not_cascade(self) -> None:
        """保留成本记录：job 被清理不应连带删除调用记录。"""
        fk = next(iter(table("provider_calls").c.job_id.foreign_keys))
        assert fk.ondelete == "SET NULL"


class TestAuditEventConstraints:
    def test_append_only_shape(self) -> None:
        columns = set(table("audit_events").columns.keys())
        assert "deleted_at" not in columns
        assert "updated_by" not in columns
        assert "status" not in columns

    def test_version_fields_are_not_named_without_scope(self) -> None:
        """before_version / after_version 专指 resources.version。"""
        columns = set(table("audit_events").columns.keys())
        assert {"before_version", "after_version"}.issubset(columns)
        assert "acl_version_before" not in columns  # ACL 前后值进 metadata

    def test_actor_is_set_null_on_delete(self) -> None:
        fk = next(iter(table("audit_events").c.actor_id.foreign_keys))
        assert fk.ondelete == "SET NULL"

    def test_action_not_empty(self) -> None:
        assert checks("audit_events")["ck_audit_events_action_not_empty"] == "length(action) > 0"

    def test_lookup_indexes(self) -> None:
        names = {i.name for i in table("audit_events").indexes}
        assert "ix_audit_events_resource_id_created_at" in names
        assert "ix_audit_events_actor_id_created_at" in names
        assert "ix_audit_events_action_created_at" in names


class TestActionConstraints:
    def test_idempotency_identity(self) -> None:
        assert "uq_actions_idempotency_identity" in uniques("actions")
        unique = next(
            c
            for c in table("actions").constraints
            if isinstance(c, UniqueConstraint) and c.name == "uq_actions_idempotency_identity"
        )
        assert [c.name for c in unique.columns] == [
            "run_id",
            "kind",
            "target_id",
            "target_version",
            "args_hash",
        ]

    def test_kind_and_status_checks(self) -> None:
        kind_expression = checks("actions")["ck_actions_kind_valid"]
        for value in ActionKind:
            assert f"'{value.value}'" in kind_expression
        status_expression = checks("actions")["ck_actions_status_valid"]
        for value in ActionStatus:
            assert f"'{value.value}'" in status_expression

    def test_target_version_is_bound(self) -> None:
        assert "target_version" in table("actions").columns
        assert "args_hash" in table("actions").columns

    def test_status_timestamps_use_containment_not_equality(self) -> None:
        """包含式约束：能表达"确认→执行"，同时仍拦住"半截转换"。

        双向等式在这里是错的：执行态同时需要 confirmed_at 与 executed_at，
        写成 ``(status = 'executed') = (confirmed_at IS NOT NULL)`` 会直接拒绝正常流程。
        """
        expressions = checks("actions")
        assert (
            expressions["ck_actions_confirmed_at_matches_status"]
            == "(confirmed_at IS NULL) = (status NOT IN ('confirmed', 'executed'))"
        )
        assert (
            expressions["ck_actions_executed_at_matches_status"]
            == "(executed_at IS NULL) = (status <> 'executed')"
        )
        assert (
            expressions["ck_actions_rejected_at_matches_status"]
            == "(rejected_at IS NULL) = (status <> 'rejected')"
        )
        assert (
            expressions["ck_actions_expired_at_matches_status"]
            == "(expired_at IS NULL) = (status <> 'expired')"
        )
        columns = set(table("actions").columns.keys())
        assert {"confirmed_at", "executed_at", "rejected_at", "expired_at"}.issubset(columns)
        # 确认态与执行态都要求 confirmed_at，因此不再需要单独的单向蕴含约束。
        assert "ck_actions_executed_implies_confirmed" not in expressions

    def test_expiry_after_creation(self) -> None:
        assert "expires_at > created_at" in checks("actions")["ck_actions_expires_after_created"]

    def test_expiry_lookup_index(self) -> None:
        index = index_by_name("actions", "ix_actions_status_expires_at")
        assert [c.name for c in index.columns] == ["status", "expires_at"]


class TestInPredicateHelper:
    def test_produces_literal_in_predicate(self) -> None:
        assert in_predicate("status", ["a", "b"]) == "status IN ('a', 'b')"

    def test_rejects_empty_input(self) -> None:
        with pytest.raises(ValueError, match="至少要有一个取值"):
            in_predicate("status", [])
