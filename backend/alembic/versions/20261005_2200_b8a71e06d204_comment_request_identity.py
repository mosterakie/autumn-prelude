"""Persist original comment request identity without rewriting existing revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8a71e06d204"
down_revision: str | None = "c8dc294b55a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("comments", sa.Column("request_hash", sa.Text(), nullable=True))
    # Python canonical JSON: sorted keys, UTF-8, separators=(",", ":"), only CR normalization.
    op.execute(r"""
        CREATE FUNCTION autumn_comment_request_hash(resource uuid, parent uuid, body text)
        RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
            SELECT encode(sha256(convert_to(
                '{"body":' || to_json(replace(replace(body, E'\r\n', E'\n'), E'\r', E'\n'))::text
                || ',"parent_id":' || coalesce(to_json(parent::text)::text, 'null')
                || ',"resource_id":' || coalesce(to_json(resource::text)::text, 'null') || '}',
                'UTF8')), 'hex')
        $$
    """)
    op.execute(
        "UPDATE comments SET request_hash = autumn_comment_request_hash(resource_id, parent_id, body)"
    )
    op.alter_column("comments", "request_hash", nullable=False)
    op.create_check_constraint("request_hash_shape", "comments", "request_hash ~ '^[0-9a-f]{64}$'")
    # Legacy/raw SQL inserts receive the same identity; future body edits retain the original hash.
    op.execute("""
        CREATE FUNCTION autumn_set_comment_request_hash() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.request_hash := autumn_comment_request_hash(NEW.resource_id, NEW.parent_id, NEW.body);
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_comments_request_hash BEFORE INSERT ON comments FOR EACH ROW EXECUTE FUNCTION autumn_set_comment_request_hash()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_comments_request_hash ON comments")
    op.execute("DROP FUNCTION autumn_set_comment_request_hash()")
    op.drop_constraint(op.f("ck_comments_request_hash_shape"), "comments", type_="check")
    op.drop_column("comments", "request_hash")
    op.execute("DROP FUNCTION autumn_comment_request_hash(uuid, uuid, text)")
