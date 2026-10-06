from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from autumn_backend.db.enums import JobStatus, ProviderCallPurpose, ProviderCallStatus, RunStatus
from autumn_backend.db.models import Job, QuotaReservation
from autumn_backend.errors import ConflictError, LeaseLostError, OptimisticLockError
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.chats import ChatService
from autumn_backend.services.execution import ExecutionService
from autumn_backend.services.maintenance import MaintenanceService
from autumn_backend.services.provider_reconciliation import ProviderReconciliationService, Receipt
from autumn_backend.services.run_cancellation import RunCancellationService
from autumn_backend.services.runtime import RuntimeService
from autumn_backend.services.storage import StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.workers.bootstrap import configured_worker
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import service_for
from tests.integration.test_storage_service import ready_file, store_for_test

pytestmark = pytest.mark.integration


async def test_crash_reclaim_preserves_unknown_charge_and_requeues_safe_job(
    e_case: ServiceCase,
) -> None:
    job_id, token = await accepted_job(e_case)
    ticket = await RuntimeService(e_case.uows).open(job_id, token)
    await service_for(e_case).capture_dependencies(ticket.actor, ticket.run_id)
    async with e_case.uows() as uow:
        call = (
            await uow.repositories.provider_calls.prepare(
                provider="test",
                model="fixed",
                purpose=ProviderCallPurpose.CHAT,
                logical_call_key=uuid4().hex,
                run_id=ticket.run_id,
                job_id=job_id,
            )
        ).record
        safe = (
            await uow.repositories.jobs.enqueue(
                JobSpec(
                    kind="storage.finalize",
                    idempotency_key=uuid4().hex,
                    payload={},
                    actor_id=e_case.owner.user_id,
                    auth_session_id=e_case.owner.auth_session_id,
                )
            )
        ).record
        safe = await uow.repositories.jobs.claim(kinds=("storage.finalize",))
        safe_id = safe.id
    await ExecutionService(e_case.uows).start_model_call(ticket.actor, job_id, token, call.id)
    async with e_case.uows() as uow:
        await uow.session.execute(
            update(Job)
            .where(Job.id.in_((job_id, safe_id)))
            .values(
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    await MaintenanceService(e_case.uows).tick()
    async with e_case.uows() as uow:
        assert (await uow.repositories.jobs.get_or_raise(safe_id)).status is JobStatus.QUEUED
        assert (await uow.repositories.jobs.get_or_raise(job_id)).status is JobStatus.FAILED
        unknown = await uow.repositories.provider_calls.get_or_raise(call.id)
        assert unknown.status is ProviderCallStatus.UNKNOWN and unknown.actual_cost is None
        run = await uow.repositories.runs.get_or_raise(ticket.run_id)
        assert run.status is RunStatus.FAILED and run.execution_generation > ticket.generation
        assert (await uow.repositories.quota_reservations.for_run(run.id)).status.value == "charged"
        with pytest.raises(LeaseLostError):
            await uow.repositories.jobs.finish(job_id, token)


async def test_owner_receipt_reconciliation_run_only_fence_and_idempotence(
    e_case: ServiceCase,
) -> None:
    job_id, token = await accepted_job(e_case)
    ticket = await RuntimeService(e_case.uows).open(job_id, token)
    async with e_case.uows() as uow:
        call = (
            await uow.repositories.provider_calls.prepare(
                provider="test",
                purpose=ProviderCallPurpose.EMBEDDING,
                logical_call_key=uuid4().hex,
                run_id=ticket.run_id,
            )
        ).record
        await uow.repositories.provider_calls.mark_dispatched(call.id)
        call = await uow.repositories.provider_calls.settle_unknown(call.id)
        run = await uow.repositories.runs.get_or_raise(ticket.run_id)
        receipt = Receipt(
            expected_version=call.version,
            expected_run_version=run.version,
            expected_generation=run.execution_generation,
            status="succeeded",
            external_request_id="verified-test-receipt",
            evidence_sha256="a" * 64,
            actual_cost=Decimal("0.13"),
            currency="USD",
        )
    service = ProviderReconciliationService(e_case.uows)
    assert (await service.candidates(e_case.owner))[0]["id"] == str(call.id)
    with pytest.raises(AuthorizationError):
        await service.confirm(e_case.member, call.id, receipt)
    with pytest.raises(OptimisticLockError):
        await service.confirm(
            e_case.owner,
            call.id,
            receipt.model_copy(update={"expected_generation": ticket.generation + 1}),
        )
    result = await service.confirm(e_case.owner, call.id, receipt)
    assert await service.confirm(e_case.owner, call.id, receipt) == result
    with pytest.raises(ConflictError):
        await service.confirm(
            e_case.owner, call.id, receipt.model_copy(update={"actual_cost": Decimal("0.14")})
        )


async def test_reservation_cleanup_keeps_active_run_and_retries_pending_delete(
    e_case: ServiceCase,
) -> None:
    storage = StorageService(e_case.uows, store_for_test())
    file = await ready_file(e_case, storage)
    await storage.delete(e_case.owner, file.id)
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("storage.delete",))
        leased = LeasedJob(job.id, job.kind, job.lease_token)
        failed = await e_case.run(uow)
        failed.status, failed.finished_at = (
            RunStatus.FAILED,
            await uow.repositories.runs.database_time(),
        )
        failed.version += 1
        active = await e_case.run(uow)
        await uow.session.flush()
        await uow.session.execute(
            update(QuotaReservation)
            .where(QuotaReservation.run_id.in_((failed.id, active.id)))
            .values(
                created_at=func.clock_timestamp() - timedelta(hours=2),
            )
        )
    await TaskService(e_case.uows).failure(leased, RuntimeError("retryable file I/O"))
    await MaintenanceService(e_case.uows).tick()
    async with e_case.uows() as uow:
        assert (
            await uow.repositories.quota_reservations.for_run(failed.id)
        ).status.value == "released"
        assert (
            await uow.repositories.quota_reservations.for_run(active.id)
        ).status.value == "reserved"
        retry = await uow.repositories.jobs.get_or_raise(leased.id)
        assert (
            retry.status is JobStatus.QUEUED
            and retry.auth_session_id == e_case.owner.auth_session_id
        )
        assert (await uow.repositories.files.get_or_raise(file.id)).deleted_at is None
        await uow.session.execute(
            update(Job).where(Job.id == retry.id).values(available_at=func.clock_timestamp())
        )
    assert await configured_worker(e_case.uows, storage).run_once()
    async with e_case.uows() as uow:
        assert (await uow.repositories.files.get_or_raise(file.id)).deleted_at is not None


async def test_cancel_handler_and_deleted_conversation_cleanup(e_case: ServiceCase) -> None:
    job_id, token = await accepted_job(e_case)
    ticket = await RuntimeService(e_case.uows).open(job_id, token)
    await service_for(e_case).capture_dependencies(ticket.actor, ticket.run_id)
    async with e_case.uows() as uow:
        call = (
            await uow.repositories.provider_calls.prepare(
                provider="test",
                model="fixed",
                purpose=ProviderCallPurpose.CHAT,
                logical_call_key=uuid4().hex,
                run_id=ticket.run_id,
                job_id=job_id,
            )
        ).record
    await ExecutionService(e_case.uows).start_model_call(ticket.actor, job_id, token, call.id)
    await ChatService(e_case.uows).cancel(ticket.actor, ticket.run_id)
    async with e_case.uows() as uow:
        cancel_job = await uow.repositories.jobs.claim(kinds=("run.cancel",))
    requested = []

    async def cancel(provider: str, model: str, key: str) -> None:
        requested.append((provider, model, key))

    await RunCancellationService(e_case.uows, cancel).execute(
        LeasedJob(cancel_job.id, cancel_job.kind, cancel_job.lease_token)
    )
    assert requested[0][:2] == ("test", "fixed") and requested[0][2].startswith("autumn-")
    await TaskService(e_case.uows).failure(
        LeasedJob(job_id, "run.dispatch", token), OptimisticLockError("old generation")
    )
    async with e_case.uows() as uow:
        assert (
            await uow.repositories.runs.get_or_raise(ticket.run_id)
        ).status is RunStatus.CANCELLED
        assert (
            await uow.repositories.provider_calls.get_or_raise(call.id)
        ).status is ProviderCallStatus.UNKNOWN
        run = await uow.repositories.runs.get_or_raise(ticket.run_id)
        conversation = await uow.repositories.conversations.get_or_raise(run.conversation_id)
        conversation_id, version = conversation.id, conversation.version
    await ChatService(e_case.uows).delete(ticket.actor, conversation_id, version=version)
    assert await configured_worker(e_case.uows, service_for(e_case).storage).run_once()
    async with e_case.uows() as uow:
        cleanup = await uow.session.scalar(select(Job).where(Job.kind == "conversation.cleanup"))
        assert cleanup.status is JobStatus.SUCCEEDED
