"""真实数据库认证；邮件只持久入队，密码计算不跨事务。"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from autumn_backend.auth.contracts import SessionView
from autumn_backend.auth.security import (
    AuthError,
    SecretBox,
    csrf_signature,
    hash_password,
    normalize_email,
    random_token,
    token_hash,
    totp_step,
    verify_password,
)
from autumn_backend.config import Settings
from autumn_backend.db.enums import AuditResult, AuthTokenPurpose, UserRole, UserStatus
from autumn_backend.db.models import AdminFactor, AuthSession, AuthToken, User
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.policies import ActorContext, ActorRole, capabilities_for
from autumn_backend.policies.access import authentication_denial
from autumn_backend.policies.facts import Operation
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.access import lock_authentication, publication_facts
from autumn_backend.services.ai_limits import read_ai_limits


@dataclass(frozen=True, slots=True)
class LoginResult:
    session: SessionView
    token: str


class AuthService:
    def __init__(self, uows: UnitOfWorkFactory, settings: Settings) -> None:
        self.uows = uows
        self.settings = settings
        self.box = SecretBox(settings.auth_encryption_key.get_secret_value())

    async def _throttle(self, *, action: str, identity: str, remote_address: str) -> None:
        # 单独提交，让失败登录、未知邮箱与无效 token 也消耗尝试次数。
        async with self.uows() as uow:
            now = await uow.repositories.users.database_time()
            minutes = 15
            start = now.replace(minute=now.minute // minutes * minutes, second=0, microsecond=0)
            end = start + timedelta(minutes=minutes)
            scopes = (
                ("identity", identity, 10 if action in {"login", "step-up", "token"} else 3),
                ("address", remote_address, 60 if action in {"login", "step-up", "token"} else 20),
            )
            for kind, value, limit in sorted(scopes):
                digest = hmac.new(
                    self.settings.session_secret.get_secret_value().encode(),
                    f"auth:{action}:{kind}:{value}".encode(),
                    hashlib.sha256,
                ).hexdigest()
                await uow.repositories.rate_limits.consume(
                    scope_hash=digest,
                    policy_key=f"auth.{action}.{kind}",
                    window_start=start,
                    window_end=end,
                    limit=limit,
                )

    def verify_origin(self, origin: str | None) -> None:
        if origin is None or origin not in self.settings.trusted_origins:
            raise AuthError("origin_forbidden")

    def verify_write(
        self, session: SessionView, *, origin: str | None, csrf_token: str | None
    ) -> None:
        self.verify_origin(origin)
        if csrf_token is None or not hmac.compare_digest(session.csrf_token, csrf_token):
            raise AuthError("csrf_invalid")

    async def _view(self, uow: UnitOfWork, user: User, session: AuthSession) -> SessionView | None:
        actor = ActorContext(
            user_id=user.id,
            role=ActorRole(user.role.value),
            auth_session_id=session.id,
            step_up_expires_at=session.step_up_expires_at if user.role is UserRole.OWNER else None,
            capabilities=frozenset(),
            scope_epoch=await uow.repositories.settings.get_acl_epoch(),
        )
        facts = await publication_facts(
            uow, Operation.READ_CONVERSATION, await lock_authentication(uow, actor), None
        )
        if authentication_denial(actor, facts) is not None:
            return None
        capabilities = capabilities_for(actor, facts)
        actor = ActorContext(
            user_id=actor.user_id,
            role=actor.role,
            auth_session_id=actor.auth_session_id,
            step_up_expires_at=actor.step_up_expires_at,
            capabilities=capabilities,
            scope_epoch=actor.scope_epoch,
        )
        dto: dict[str, Any] = {
            "id": user.id,
            "display_name": user.display_name,
            "email": user.email_normalized,
            "role": user.role.value,
            "status": user.status.value,
            "verified_at": user.verified_at,
            "ai_cooldown_until": user.ai_cooldown_until,
            "step_up_expires_at": session.step_up_expires_at,
            "capabilities": sorted(capability.value for capability in capabilities),
        }
        return SessionView(
            actor,
            dto,
            csrf_signature(
                self.settings.csrf_secret.get_secret_value(), session.id, session.csrf_version
            ),
            session.csrf_version,
        )

    async def authenticate(self, token: str) -> SessionView | None:
        if not 40 <= len(token) <= 128:
            return None
        async with self.uows() as uow:
            observed = await uow.repositories.auth_sessions.by_token_hash(token_hash(token))
            if observed is None:
                return None
            user = await uow.repositories.users.get_for_update(observed.user_id)
            session = await uow.repositories.auth_sessions.for_user_for_update(
                observed.id, observed.user_id
            )
            if user is None or session is None:
                return None
            return await self._view(uow, user, session)

    async def _new_session(
        self,
        uow: UnitOfWork,
        user: User,
        *,
        previous: UUID | None = None,
        step_up: datetime | None = None,
    ) -> LoginResult:
        now = await uow.repositories.users.database_time()
        version = await uow.repositories.auth_sessions.next_csrf_version(user.id)
        if previous is not None:
            old = await uow.repositories.auth_sessions.for_user_for_update(previous, user.id)
            if old is not None and old.revoked_at is None:
                old.revoked_at, old.csrf_version, old.version = (
                    now,
                    old.csrf_version + 1,
                    old.version + 1,
                )
        raw = random_token()
        absolute = now + timedelta(hours=self.settings.session_ttl_hours)
        session = AuthSession(
            user_id=user.id,
            token_hash=token_hash(raw),
            auth_version=user.auth_version,
            csrf_version=version,
            last_seen_at=now,
            absolute_expires_at=absolute,
            idle_expires_at=min(absolute, now + timedelta(hours=self.settings.session_idle_hours)),
            step_up_expires_at=step_up,
        )
        uow.session.add(session)
        await uow.session.flush()
        view = await self._view(uow, user, session)
        if view is None:
            raise AuthError("invalid_credentials")
        await uow.repositories.audit_events.record(
            event_type="auth.step_up" if step_up else "auth.login",
            result=AuditResult.SUCCEEDED,
            actor_id=user.id,
        )
        return LoginResult(view, raw)

    async def _issue(self, uow: UnitOfWork, user: User, purpose: AuthTokenPurpose) -> None:
        raw = random_token()
        now = await uow.repositories.users.database_time()
        token = AuthToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=token_hash(raw),
            expires_at=now + timedelta(hours=24 if purpose is AuthTokenPurpose.VERIFY_EMAIL else 1),
        )
        uow.session.add(token)
        await uow.session.flush()
        await uow.repositories.jobs.enqueue(
            JobSpec(
                kind="auth.email",
                idempotency_key=f"auth.email:{token.id}",
                actor_id=user.id,
                payload={
                    "schema_version": 1,
                    "token_id": str(token.id),
                    "purpose": purpose.value,
                    "encryption_key_version": 1,
                    "ciphertext": self.box.seal(raw),
                },
            )
        )

    async def register(
        self, *, email: str, password: str, display_name: str | None, remote_address: str
    ) -> None:
        email = normalize_email(email)
        if display_name is not None and not 1 <= len(display_name.strip()) <= 80:
            raise InvalidInputError("显示名称无效")
        await self._throttle(action="mail", identity=email, remote_address=remote_address)
        encoded = await hash_password(password)
        async with self.uows() as uow:
            user = await uow.repositories.users.register(
                email=email,
                password_hash=encoded,
                display_name=display_name.strip() if display_name else None,
            )
            if user is not None:
                await self._issue(uow, user, AuthTokenPurpose.VERIFY_EMAIL)
                await uow.repositories.audit_events.record(
                    event_type="auth.register", result=AuditResult.SUCCEEDED, actor_id=user.id
                )

    async def request_email(
        self, *, email: str, purpose: AuthTokenPurpose, remote_address: str
    ) -> None:
        email = normalize_email(email)
        await self._throttle(action="mail", identity=email, remote_address=remote_address)
        async with self.uows() as uow:
            observed = await uow.repositories.users.by_email(email)
            if observed is None:
                return
            user = await uow.repositories.users.get_for_update(observed.id)
            assert user is not None
            if user.deleted_at is not None or user.status is UserStatus.DISABLED:
                return
            if purpose is AuthTokenPurpose.VERIFY_EMAIL and user.verified_at is not None:
                return
            await self._issue(uow, user, purpose)

    async def resend_verification(self, *, email: str, remote_address: str) -> None:
        await self.request_email(
            email=email, purpose=AuthTokenPurpose.VERIFY_EMAIL, remote_address=remote_address
        )

    async def forgot_password(self, *, email: str, remote_address: str) -> None:
        await self.request_email(
            email=email, purpose=AuthTokenPurpose.RESET_PASSWORD, remote_address=remote_address
        )

    async def verify_email(self, *, raw: str, remote_address: str) -> None:
        await self.consume_token(
            raw=raw, purpose=AuthTokenPurpose.VERIFY_EMAIL, remote_address=remote_address
        )

    async def reset_password(self, *, raw: str, new_password: str, remote_address: str) -> None:
        await self.consume_token(
            raw=raw,
            purpose=AuthTokenPurpose.RESET_PASSWORD,
            remote_address=remote_address,
            new_password=new_password,
        )

    async def login(
        self, *, email: str, password: str, remote_address: str, previous: UUID | None = None
    ) -> LoginResult:
        email = normalize_email(email)
        if len(password) > 256:
            raise AuthError("invalid_credentials")
        await self._throttle(action="login", identity=email, remote_address=remote_address)
        async with self.uows() as uow:
            observed = await uow.repositories.users.by_email(email)
            captured = (
                None
                if observed is None
                else (observed.id, observed.password_hash, observed.auth_version)
            )
        if (
            not await verify_password(password, captured[1] if captured else None)
            or captured is None
        ):
            raise AuthError("invalid_credentials")
        async with self.uows() as uow:
            user = await uow.repositories.users.get_for_update(captured[0])
            if (
                user is None
                or user.password_hash != captured[1]
                or user.auth_version != captured[2]
                or user.status is UserStatus.DISABLED
                or user.deleted_at is not None
            ):
                raise AuthError("invalid_credentials")
            if user.role is UserRole.OWNER:
                factor = await uow.repositories.auth_credentials.factor(user.id)
                if factor is None or factor.enabled_at is None or factor.revoked_at is not None:
                    raise AuthError("invalid_credentials")
            return await self._new_session(uow, user, previous=previous)

    async def logout(self, actor: ActorContext) -> None:
        async with self.uows() as uow:
            await lock_authentication(uow, actor)
            if actor.user_id is None or actor.auth_session_id is None:
                return
            session = await uow.repositories.auth_sessions.for_user_for_update(
                actor.auth_session_id, actor.user_id
            )
            if session is not None and session.revoked_at is None:
                session.revoked_at = await uow.repositories.users.database_time()
                session.csrf_version += 1
                session.version += 1
                await uow.repositories.audit_events.record(
                    event_type="auth.logout", result=AuditResult.SUCCEEDED, actor_id=actor.user_id
                )

    async def consume_token(
        self,
        *,
        raw: str,
        purpose: AuthTokenPurpose,
        remote_address: str,
        new_password: str | None = None,
    ) -> None:
        if not 40 <= len(raw) <= 128:
            raise AuthError("invalid_token")
        await self._throttle(
            action="token", identity=token_hash(raw), remote_address=remote_address
        )
        encoded = await hash_password(new_password) if new_password is not None else None
        async with self.uows() as uow:
            observed = await uow.repositories.auth_credentials.token(token_hash(raw), purpose)
            if observed is None:
                raise AuthError("invalid_token")
            user = await uow.repositories.users.get_for_update(observed.user_id)
            token = await uow.repositories.auth_credentials.token(
                token_hash(raw), purpose, lock=True
            )
            now = await uow.repositories.users.database_time()
            if (
                user is None
                or token is None
                or token.consumed_at is not None
                or token.expires_at <= now
                or user.status is UserStatus.DISABLED
                or user.deleted_at is not None
            ):
                raise AuthError("invalid_token")
            token.consumed_at, token.version = now, token.version + 1
            if purpose is AuthTokenPurpose.VERIFY_EMAIL:
                if user.verified_at is None:
                    limits = read_ai_limits(await uow.repositories.settings.get("ai_limits"))
                    user.verified_at, user.status = now, UserStatus.ACTIVE
                    user.ai_cooldown_until = now + timedelta(
                        hours=limits.for_role(ActorRole(user.role.value)).cooldown_hours
                    )
            else:
                if encoded is None:
                    raise InvalidInputError("缺少新密码")
                user.password_hash, user.auth_version = encoded, user.auth_version + 1
                await uow.repositories.auth_sessions.revoke_all(user.id)
            user.version += 1
            await uow.repositories.auth_credentials.scrub_mail(token.id)
            await uow.repositories.auth_credentials.invalidate_other_tokens(
                user.id, purpose, token.id
            )
            await uow.repositories.audit_events.record(
                event_type="auth.verify_email"
                if purpose is AuthTokenPurpose.VERIFY_EMAIL
                else "auth.reset_password",
                result=AuditResult.SUCCEEDED,
                actor_id=user.id,
            )

    async def step_up(
        self,
        actor: ActorContext,
        *,
        code: str | None,
        recovery_code: str | None,
        remote_address: str,
    ) -> LoginResult:
        if (code is None) == (recovery_code is None):
            raise InvalidInputError("验证代码与恢复码必须二选一")
        if (
            actor.user_id is None
            or actor.auth_session_id is None
            or actor.role is not ActorRole.OWNER
        ):
            raise AuthError("step_up_required")
        await self._throttle(
            action="step-up", identity=str(actor.user_id), remote_address=remote_address
        )
        async with self.uows() as uow:
            auth = await lock_authentication(uow, actor)
            facts = await publication_facts(uow, Operation.READ_CONVERSATION, auth, None)
            if (
                authentication_denial(actor, facts) is not None
                or auth is None
                or auth.verified_at is None
            ):
                raise AuthError("invalid_credentials")
            factor = await uow.repositories.auth_credentials.factor(actor.user_id, lock=True)
            if (
                factor is None
                or factor.enabled_at is None
                or factor.revoked_at is not None
                or factor.encryption_key_version != 1
            ):
                raise AuthError("invalid_credentials")
            now = await uow.repositories.users.database_time()
            if code is not None:
                used = totp_step(
                    self.box.open(factor.secret_ciphertext),
                    code,
                    int(now.timestamp()) // 30,
                    factor.last_used_time_step,
                )
                if used is None:
                    raise AuthError("invalid_credentials")
                factor.last_used_time_step = used
            else:
                assert recovery_code is not None
                values = dict(factor.recovery_code_hashes or {})
                digest = token_hash(recovery_code.strip())
                matched = next(
                    (value for value in values if hmac.compare_digest(value, digest)), None
                )
                if matched is None or values[matched] is not False:
                    raise AuthError("invalid_credentials")
                values[matched] = True
                factor.recovery_code_hashes = values
            factor.version += 1
            user = await uow.repositories.users.get_for_update(actor.user_id)
            assert user is not None
            return await self._new_session(
                uow,
                user,
                previous=actor.auth_session_id,
                step_up=now + timedelta(minutes=self.settings.step_up_minutes),
            )

    async def bootstrap_owner(
        self, *, email: str, password: str, display_name: str, totp_secret: str, code: str
    ) -> tuple[str, ...]:
        """仅本地 CLI 调用；不存在任何 HTTP/Agent 注册站长入口。"""
        email = normalize_email(email)
        if not 1 <= len(display_name.strip()) <= 80:
            raise InvalidInputError("显示名称无效")
        encoded = await hash_password(password)
        recovery = tuple(random_token() for _ in range(8))
        async with self.uows() as uow:
            if (
                not await uow.repositories.users.lock_owner_bootstrap()
                or await uow.repositories.users.by_email(email)
            ):
                raise ConflictError("站长已存在或邮箱已注册")
            now = await uow.repositories.users.database_time()
            step = totp_step(totp_secret, code, int(now.timestamp()) // 30, None)
            if step is None:
                raise AuthError("invalid_credentials")
            user = User(
                email_normalized=email,
                display_name=display_name.strip(),
                password_hash=encoded,
                role=UserRole.OWNER,
                status=UserStatus.ACTIVE,
                verified_at=now,
                ai_cooldown_until=now,
            )
            uow.session.add(user)
            await uow.session.flush()
            uow.session.add(
                AdminFactor(
                    user_id=user.id,
                    secret_ciphertext=self.box.seal(totp_secret),
                    enabled_at=now,
                    last_used_time_step=step,
                    recovery_code_hashes={token_hash(value): False for value in recovery},
                )
            )
            await uow.repositories.audit_events.record(
                event_type="auth.bootstrap", result=AuditResult.SUCCEEDED, actor_id=user.id
            )
        return recovery

    @staticmethod
    def new_totp_secret() -> str:
        return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
