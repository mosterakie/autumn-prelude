"""一次性探针：直接验证高风险的新约束是否真的生效。

不复用测试套件——A 阶段的教训是"本地说绿"必须能被独立复核。
所有写入都在一个外层事务内执行并最终回滚，不改动数据库内容。

三个必须注意的点（前一版探针在这三处都踩过）：

1. **可延迟外键**（`resources.current_revision_id`、`runs.input_message_id` /
   `current_message_id`）默认在**提交时**才校验，因此在 SAVEPOINT 里插入违规行
   不会立刻报错。断言前必须 ``SET CONSTRAINTS ALL IMMEDIATE``。
2. **断言用的写入要回滚，但前置行必须留存**：否则"重复唯一键"这类断言
   永远看到空表。
3. **参数类型**：`text[]` 传 Python 列表；同一个占位符出现在两种推导类型下
   （如既是 timestamptz 又参与 interval 运算）会触发
   ``AmbiguousParameterError``，需要显式类型转换。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg


def _dsn() -> str:
    """探针连的库：优先测试库，其次应用库；由环境决定，便于 CI 复用。"""
    url = os.environ.get("AUTUMN_TEST_DATABASE_URL") or os.environ.get(
        "AUTUMN_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5442/autumn"
    )
    return url.replace("+asyncpg", "")


_INSERT_USER = """
INSERT INTO users (email_normalized, password_hash, role, status, auth_version, version)
VALUES ($1, 'h', 'member', 'active', 1, 0) RETURNING id
"""

_INSERT_RESOURCE = """
INSERT INTO resources (owner_id, kind, slug, acl_version, version)
VALUES ($1, 'article', $2, 0, 0) RETURNING id
"""

_INSERT_VERSION = """
INSERT INTO resource_versions (resource_id, revision_no, content_format, tags, version)
VALUES ($1, $2, 'markdown', '{}', 0) RETURNING id
"""

_INSERT_PUBLICATION = """
INSERT INTO publications (resource_id, revision_id, publication_no, public_title, public_fields,
                          published_by, ai_enabled, raw_download_enabled, version)
VALUES ($1, $2, $3, $4, $5, $6, false, false, 0) RETURNING id
"""

_INSERT_INDEX = """
INSERT INTO knowledge_indexes (resource_id, revision_id, publication_id, scope,
                               embedding_provider, embedding_model, embedding_dimension,
                               generation, status, is_active, content_hash, version)
VALUES ($1, $2, $3, $4, 'bailian', 'm', $5, 1, $6, $7, 'h', 0)
"""

_INSERT_CONVERSATION = """
INSERT INTO conversations (user_id, mode, title, next_message_seq, version)
VALUES ($1, 'owner', '', 1, 0) RETURNING id
"""

_INSERT_RUN = """
INSERT INTO runs (user_id, conversation_id, idempotency_key, request_hash, status, scope_epoch,
                  checkpoint_thread_id, next_event_seq, config_snapshot, version)
VALUES ($1, $2, $3, 'r', 'queued', 0, 'thread', 1, '{}', 0) RETURNING id
"""

_INSERT_RESERVATION = """
INSERT INTO quota_reservations (run_id, bucket_id, user_id, amount, status, version)
VALUES ($1, $2, $3, 1, 'reserved', 0)
"""

_INSERT_MESSAGE = """
INSERT INTO messages (conversation_id, seq, role, body_text, content_version, status,
                      client_message_id, version)
VALUES ($1, $2, 'user', $3, 1, 'complete', $4, 0)
"""

_failures: list[str] = []


async def _rejected(
    conn: asyncpg.Connection, label: str, sql: str, *args: object, force_deferred: bool = False
) -> None:
    """在 SAVEPOINT 内执行，期望被数据库拒绝。"""
    await conn.execute("SAVEPOINT probe")
    try:
        await conn.execute(sql, *args)
        if force_deferred:
            # 可延迟外键只在提交时校验；这里提前触发。
            await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except asyncpg.PostgresError as error:
        await conn.execute("ROLLBACK TO SAVEPOINT probe")
        print(f"  [拒绝 OK] {label}: {type(error).__name__}")
        return
    await conn.execute("ROLLBACK TO SAVEPOINT probe")
    _failures.append(label)
    print(f"  [!! 未被拒绝] {label}")


async def _accepted(conn: asyncpg.Connection, label: str, sql: str, *args: object) -> None:
    await conn.execute("SAVEPOINT probe")
    try:
        await conn.execute(sql, *args)
    except asyncpg.PostgresError as error:
        await conn.execute("ROLLBACK TO SAVEPOINT probe")
        _failures.append(label)
        print(f"  [!! 意外拒绝] {label}: {type(error).__name__}: {error}")
        return
    await conn.execute("ROLLBACK TO SAVEPOINT probe")
    print(f"  [接受 OK] {label}")


async def main() -> int:
    conn = await asyncpg.connect(_dsn())
    await conn.execute("BEGIN")
    try:
        user = await conn.fetchval(_INSERT_USER, f"probe-{uuid.uuid4()}@example.com")
        other = await conn.fetchval(_INSERT_USER, f"probe-{uuid.uuid4()}@example.com")
        r1 = await conn.fetchval(_INSERT_RESOURCE, user, f"slug-{uuid.uuid4()}")
        r2 = await conn.fetchval(_INSERT_RESOURCE, user, f"slug-{uuid.uuid4()}")
        r3 = await conn.fetchval(_INSERT_RESOURCE, user, f"slug-{uuid.uuid4()}")
        v1 = await conn.fetchval(_INSERT_VERSION, r1, 1)
        v2 = await conn.fetchval(_INSERT_VERSION, r2, 1)
        v3 = await conn.fetchval(_INSERT_VERSION, r3, 1)
        # 留存：后面 knowledge_indexes 与 messages 断言要用到这些前置行。
        publication = await conn.fetchval(_INSERT_PUBLICATION, r1, v1, 1, "标题", ["title"], user)
        print("（已建用户 / 资源 / 版本 / 合法发布，全部在事务内留存）")

        print("\n[1] resources.current_revision_id 复合外键（可延迟，需强制校验）")
        await _rejected(
            conn,
            "把 r2 的版本挂到 r1.current_revision_id",
            "UPDATE resources SET current_revision_id = $1 WHERE id = $2",
            v2,
            r1,
            force_deferred=True,
        )
        await _accepted(
            conn,
            "把 r1 自己的版本挂到 r1.current_revision_id",
            "UPDATE resources SET current_revision_id = $1 WHERE id = $2",
            v1,
            r1,
        )

        print("\n[2] publications：public_fields 白名单与字段列一致")
        # r1 已经有一条现行发布，因此后续正向用例换用还没有发布的资源。
        await _rejected(
            conn, "无 title 却填了 public_title", _INSERT_PUBLICATION, r1, v1, 2, "越界", [], user
        )
        await _rejected(
            conn, "非法字段名 secret", _INSERT_PUBLICATION, r1, v1, 2, None, ["secret"], user
        )
        await _accepted(
            conn,
            "['title'] 配非空标题",
            _INSERT_PUBLICATION,
            r2,
            v2,
            1,
            "公开标题",
            ["title"],
            user,
        )
        await _rejected(
            conn, "revision 属于别的资源", _INSERT_PUBLICATION, r1, v2, 4, None, [], user
        )
        await _accepted(
            conn, "另一个资源用同一 publication_no", _INSERT_PUBLICATION, r3, v3, 1, None, [], user
        )

        print("\n[3] knowledge_indexes：scope 与 publication 绑定")
        await _rejected(
            conn,
            "public 不绑 publication",
            _INSERT_INDEX,
            r1,
            v1,
            None,
            "public",
            1024,
            "ready",
            True,
        )
        await _accepted(
            conn,
            "public 绑本资源本版本 publication",
            _INSERT_INDEX,
            r1,
            v1,
            publication,
            "public",
            1024,
            "ready",
            True,
        )
        await _rejected(
            conn, "维度不是 1024", _INSERT_INDEX, r1, v1, None, "owner", 512, "ready", True
        )
        await _rejected(
            conn, "活动索引未 ready", _INSERT_INDEX, r1, v1, None, "owner", 1024, "building", True
        )
        await _accepted(
            conn,
            "owner 可以是 ready 且活动",
            _INSERT_INDEX,
            r1,
            v1,
            None,
            "owner",
            1024,
            "ready",
            True,
        )

        print("\n[4] quota_reservations：run 与桶必须属于同一用户")
        insert_bucket = (
            "INSERT INTO quota_buckets (user_id, window_start, window_end, timezone, used, "
            "reserved, policy_version, version) "
            "VALUES ($1, now(), now() + {span}, 'Asia/Shanghai', 0, 0, 0, 0) RETURNING id"
        )
        bucket = await conn.fetchval(insert_bucket.format(span="interval '1 day'"), user)
        other_bucket = await conn.fetchval(insert_bucket.format(span="interval '2 days'"), other)
        conversation = await conn.fetchval(_INSERT_CONVERSATION, user)
        run = await conn.fetchval(_INSERT_RUN, user, conversation, f"key-{uuid.uuid4()}")
        await _rejected(conn, "用别人的桶做预留", _INSERT_RESERVATION, run, other_bucket, user)
        await _accepted(conn, "用自己的桶做预留", _INSERT_RESERVATION, run, bucket, user)

        print("\n[5] runs：user_id 与 conversation_id 必须同一用户")
        other_conversation = await conn.fetchval(_INSERT_CONVERSATION, other)
        await _rejected(
            conn,
            "把别人的会话挂到我的 run",
            _INSERT_RUN,
            user,
            other_conversation,
            f"k-{uuid.uuid4()}",
        )
        # 正向用例必须**留存**，否则"第二个非终态 run"的断言看到的是空会话。
        fresh_conversation = await conn.fetchval(_INSERT_CONVERSATION, user)
        await conn.execute(_INSERT_RUN, user, fresh_conversation, f"k-{uuid.uuid4()}")
        print("  [接受 OK] 把自己的会话挂到自己的 run")
        await _rejected(
            conn,
            "同一会话第二个非终态 run",
            _INSERT_RUN,
            user,
            fresh_conversation,
            f"k-{uuid.uuid4()}",
        )

        print("\n[6] rate_limit_buckets：复合主键")
        insert_limit = (
            "INSERT INTO rate_limit_buckets (scope_hash, policy_key, window_start, window_end, "
            "hits, expires_at) "
            "VALUES ('h', 'ai_accept', $1::timestamptz, $1::timestamptz + interval '1 min', 0, "
            "now() + interval '1 hour')"
        )
        fixed_window = await conn.fetchval("SELECT now()")
        # 同上：首次插入必须留存，才能断言重复插入被拒。
        await conn.execute(insert_limit, fixed_window)
        print("  [接受 OK] 首次插入窗口计数")
        await _rejected(
            conn, "同一 (scope_hash, policy_key, window_start) 重复", insert_limit, fixed_window
        )
        await _rejected(
            conn,
            "window_end 不晚于 window_start",
            "INSERT INTO rate_limit_buckets (scope_hash, policy_key, window_start, window_end, "
            "hits, expires_at) VALUES ('h', 'login', now(), now(), 0, now() + interval '1 hour')",
        )

        print("\n[7] messages：client_message_id 部分唯一索引")
        next_seq = (
            "UPDATE conversations SET next_message_seq = next_message_seq + 1 "
            "WHERE id = $1 RETURNING next_message_seq - 1"
        )
        client_id = uuid.uuid4()
        # 前置行直接插入并留存，否则"重复键"断言看不到数据。
        first_seq = await conn.fetchval(next_seq, conversation)
        await conn.execute(_INSERT_MESSAGE, conversation, first_seq, "hi", client_id)
        second_seq = await conn.fetchval(next_seq, conversation)
        await _rejected(
            conn,
            "同一会话重复 client_message_id",
            _INSERT_MESSAGE,
            conversation,
            second_seq,
            "again",
            client_id,
        )
        await _accepted(
            conn,
            "两条都不带 client_message_id 的消息可共存",
            _INSERT_MESSAGE,
            conversation,
            second_seq + 1,
            "a",
            None,
        )
        unique_index_ok = await conn.fetchval(
            "SELECT i.indisunique FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'uq_messages_conversation_id_client_message_id'"
        )
        print(f"  [{'OK' if unique_index_ok else '!!'}] client_message_id 索引是唯一索引")
        if not unique_index_ok:
            _failures.append("client_message_id 索引不是唯一索引")

        print("\n[8] 只追加表只有 created_at")
        for table in ("run_events", "audit_events", "knowledge_chunks", "run_sources"):
            has_updated = await conn.fetchval(
                "SELECT count(1) FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = 'updated_at'",
                table,
            )
            if has_updated != 0:
                _failures.append(f"{table} 不应有 updated_at")
            print(f"  [{'OK' if has_updated == 0 else '!!'}] {table}.updated_at 不存在")

        print("\n[9] 应用表数量")
        count = await conn.fetchval(
            "SELECT count(1) FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        )
        print(f"  应用表数 = {count}（期望 28）")
        if count != 28:
            _failures.append(f"应用表数为 {count}，期望 28")
    finally:
        await conn.execute("ROLLBACK")
        await conn.close()

    if _failures:
        print(f"\n失败 {len(_failures)} 项：")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("\n全部探针通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
