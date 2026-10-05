"""Service 从当前记录组装的不可变事实；不携带 ORM 实体或私密正文。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from autumn_backend.policies._validation import (
    aware_datetime,
    boolean_value,
    nonnegative_integer,
    optional_datetime,
    uuid_value,
)
from autumn_backend.policies.actor import ActorRole


class AccountStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    DISABLED = "disabled"


class ConversationMode(StrEnum):
    PUBLIC = "public"
    OWNER = "owner"


class SearchMode(StrEnum):
    AUTO = "auto"
    SITE = "site"
    WEB = "web"


class Operation(StrEnum):
    READ_PUBLIC_RESOURCE = "read_public_resource"
    READ_PUBLIC_FILE = "read_public_file"
    READ_PUBLIC_COMMENT = "read_public_comment"
    CREATE_COMMENT = "create_comment"
    EDIT_COMMENT = "edit_comment"
    DELETE_COMMENT = "delete_comment"
    REPORT_COMMENT = "report_comment"
    CREATE_CONVERSATION = "create_conversation"
    READ_CONVERSATION = "read_conversation"
    UPDATE_CONVERSATION = "update_conversation"
    DELETE_CONVERSATION = "delete_conversation"
    ASK = "ask"
    READ_RUN = "read_run"
    CANCEL_RUN = "cancel_run"
    RESUME_RUN = "resume_run"
    READ_CITATION = "read_citation"
    SEARCH_PUBLIC_KNOWLEDGE = "search_public_knowledge"
    SEARCH_PRIVATE_KNOWLEDGE = "search_private_knowledge"
    SEARCH_WEB = "search_web"
    READ_PRIVATE_RESOURCE = "read_private_resource"
    CREATE_RESOURCE = "create_resource"
    UPDATE_RESOURCE = "update_resource"
    PUBLISH_RESOURCE = "publish_resource"
    REVOKE_PUBLICATION = "revoke_publication"
    INGEST_RESOURCE = "ingest_resource"
    READ_ACTION = "read_action"
    EXECUTE_ACTION = "execute_action"
    CANCEL_ACTION = "cancel_action"
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_MODERATION = "manage_moderation"
    READ_AUDIT = "read_audit"
    READ_MEMORY = "read_memory"
    WRITE_MEMORY = "write_memory"


class TargetKind(StrEnum):
    CONVERSATION = "conversation"
    RUN = "run"
    CITATION = "citation"
    COMMENT = "comment"
    ACTION = "action"
    MEMORY = "memory"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticationFacts:
    """当前账号与当前会话。session_user_id 保留以验证绑定关系。"""

    user_id: UUID
    session_id: UUID
    session_user_id: UUID
    role: ActorRole
    status: AccountStatus
    user_auth_version: int
    session_auth_version: int
    idle_expires_at: datetime
    absolute_expires_at: datetime
    verified_at: datetime | None = None
    ai_cooldown_until: datetime | None = None
    step_up_expires_at: datetime | None = None
    revoked_at: datetime | None = None
    user_deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("user_id", "session_id", "session_user_id"):
            uuid_value(getattr(self, name), name)
        if not isinstance(self.role, ActorRole) or self.role is ActorRole.ANONYMOUS:
            raise ValueError("authentication facts require a member or owner role")
        if not isinstance(self.status, AccountStatus):
            raise ValueError("status must be an AccountStatus")
        for name in ("user_auth_version", "session_auth_version"):
            value = getattr(self, name)
            nonnegative_integer(value, name)
            if value == 0:
                raise ValueError(f"{name} must be positive")
        aware_datetime(self.idle_expires_at, "idle_expires_at")
        aware_datetime(self.absolute_expires_at, "absolute_expires_at")
        if self.idle_expires_at > self.absolute_expires_at:
            raise ValueError("idle expiry cannot exceed absolute expiry")
        for name in (
            "verified_at",
            "ai_cooldown_until",
            "step_up_expires_at",
            "revoked_at",
            "user_deleted_at",
        ):
            optional_datetime(getattr(self, name), name)


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationFacts:
    publication_id: UUID
    resource_id: UUID
    revision_id: UUID
    is_current: bool
    ai_enabled: bool
    raw_download_enabled: bool
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("publication_id", "resource_id", "revision_id"):
            uuid_value(getattr(self, name), name)
        for name in ("is_current", "ai_enabled", "raw_download_enabled"):
            boolean_value(getattr(self, name), name)
        optional_datetime(self.revoked_at, "revoked_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceFacts:
    resource_id: UUID
    owner_id: UUID
    current_revision_id: UUID
    acl_version: int
    is_deleted: bool = False
    is_archived: bool = False
    expires_at: datetime | None = None
    publication: PublicationFacts | None = None

    def __post_init__(self) -> None:
        for name in ("resource_id", "owner_id", "current_revision_id"):
            uuid_value(getattr(self, name), name)
        nonnegative_integer(self.acl_version, "acl_version")
        boolean_value(self.is_deleted, "is_deleted")
        boolean_value(self.is_archived, "is_archived")
        optional_datetime(self.expires_at, "expires_at")
        if self.publication is not None:
            if not isinstance(self.publication, PublicationFacts):
                raise ValueError("publication must be PublicationFacts")
            if self.publication.resource_id != self.resource_id:
                raise ValueError("publication must belong to the resource")


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetFacts:
    """对象归属；citation.owner_id 是其 Run 的用户，action.owner_id 是操作者。"""

    object_id: UUID
    kind: TargetKind
    owner_id: UUID | None
    mode: ConversationMode | None = None
    is_deleted: bool = False
    expires_at: datetime | None = None
    comment_approved: bool = False
    # 留言 resource_id=None 表示留言板；关联文章时必须装配其当前 ResourceFacts。
    resource_id: UUID | None = None
    requires_step_up: bool = True

    def __post_init__(self) -> None:
        uuid_value(self.object_id, "object_id")
        if self.owner_id is not None:
            uuid_value(self.owner_id, "owner_id")
        if self.resource_id is not None:
            uuid_value(self.resource_id, "resource_id")
        if not isinstance(self.kind, TargetKind):
            raise ValueError("kind must be a TargetKind")
        if self.mode is not None and not isinstance(self.mode, ConversationMode):
            raise ValueError("mode must be a ConversationMode")
        if self.kind in (TargetKind.CONVERSATION, TargetKind.RUN, TargetKind.CITATION):
            if self.mode is None or self.owner_id is None:
                raise ValueError("chat targets require an owner and a fixed mode")
        for name in ("is_deleted", "comment_approved", "requires_step_up"):
            boolean_value(getattr(self, name), name)
        optional_datetime(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyFacts:
    """缺失对象用 None；时钟和当前 epoch 由 Service 明确传入，拒绝隐式读取。"""

    operation: Operation
    now: datetime
    current_scope_epoch: int
    authentication: AuthenticationFacts | None = None
    resource: ResourceFacts | None = None
    target: TargetFacts | None = None
    requested_mode: ConversationMode | None = None
    requested_resource_id: UUID | None = None
    search_mode: SearchMode = SearchMode.SITE

    def __post_init__(self) -> None:
        if not isinstance(self.operation, Operation):
            raise ValueError("operation must be an Operation")
        aware_datetime(self.now, "now")
        nonnegative_integer(self.current_scope_epoch, "current_scope_epoch")
        for name, expected in (
            ("authentication", AuthenticationFacts),
            ("resource", ResourceFacts),
            ("target", TargetFacts),
            ("requested_mode", ConversationMode),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected):
                raise ValueError(f"{name} has an invalid type")
        if not isinstance(self.search_mode, SearchMode):
            raise ValueError("search_mode must be a SearchMode")
        if self.requested_resource_id is not None:
            uuid_value(self.requested_resource_id, "requested_resource_id")
            if (
                self.resource is not None
                and self.requested_resource_id != self.resource.resource_id
            ):
                raise ValueError("resource must match requested_resource_id")
        if self.target is not None and self.resource is not None:
            if self.target.resource_id != self.resource.resource_id:
                raise ValueError("related resource must match target.resource_id")
