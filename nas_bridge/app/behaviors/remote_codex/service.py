"""Behavior-level facade for browser-first remote Codex work state."""

from __future__ import annotations

from ...services.remote_task_service import RemoteTaskService
from .schemas import (
    RemoteTaskApprovalRequest,
    RemoteTaskApprovalResolveRequest,
    RemoteTaskApprovalSummary,
    RemoteTaskClaimNextRequest,
    RemoteTaskClaimRequest,
    RemoteTaskCompleteRequest,
    RemoteTaskCreateRequest,
    RemoteTaskEvidenceRequest,
    RemoteTaskFailRequest,
    RemoteTaskHeartbeatRequest,
    RemoteTaskInterruptRequest,
    RemoteTaskNoteRequest,
    RemoteTaskNoteSummary,
    RemoteTaskSummaryResponse,
)


class RemoteCodexBehaviorService:
    """Thin migration facade over the current product-layer remote task service.

    This keeps the current implementation stable while giving the future
    `remote_codex` behavior a real package and service boundary inside Opscure.
    """

    behavior_id = "remote_codex"

    def __init__(self, *, remote_task_service: RemoteTaskService | None = None) -> None:
        self.remote_task_service = remote_task_service or RemoteTaskService()

    def create_task(self, payload: RemoteTaskCreateRequest) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.create_task(payload)

    def list_machine_tasks(
        self,
        *,
        machine_id: str,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[RemoteTaskSummaryResponse]:
        return self.remote_task_service.list_tasks(
            machine_id=machine_id,
            statuses=statuses,
            limit=limit,
        )

    def list_thread_tasks(
        self,
        *,
        thread_id: str,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[RemoteTaskSummaryResponse]:
        return self.remote_task_service.list_tasks(
            thread_id=thread_id,
            statuses=statuses,
            limit=limit,
        )

    def get_task(self, task_id: str) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.get_task(task_id)

    def claim_task(self, task_id: str, payload: RemoteTaskClaimRequest) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.claim_task(task_id, payload)

    def claim_next_machine_task(
        self,
        *,
        machine_id: str,
        payload: RemoteTaskClaimNextRequest,
    ) -> RemoteTaskSummaryResponse | None:
        return self.remote_task_service.claim_next_task(machine_id=machine_id, payload=payload)

    def heartbeat_task(self, task_id: str, payload: RemoteTaskHeartbeatRequest) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.heartbeat_task(task_id, payload)

    def add_evidence(self, task_id: str, payload: RemoteTaskEvidenceRequest) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.add_evidence(task_id, payload)

    def get_latest_approval(self, task_id: str) -> RemoteTaskApprovalSummary | None:
        return self.remote_task_service.get_latest_approval(task_id)

    def request_approval(
        self,
        task_id: str,
        payload: RemoteTaskApprovalRequest,
    ) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.request_approval(task_id, payload)

    def resolve_approval(
        self,
        task_id: str,
        payload: RemoteTaskApprovalResolveRequest,
    ) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.resolve_approval(task_id, payload)

    def add_note(self, task_id: str, payload: RemoteTaskNoteRequest) -> RemoteTaskNoteSummary:
        return self.remote_task_service.add_note(task_id, payload)

    def list_notes(self, task_id: str) -> list[RemoteTaskNoteSummary]:
        return self.remote_task_service.list_notes(task_id)

    def interrupt_task(
        self,
        task_id: str,
        payload: RemoteTaskInterruptRequest,
    ) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.interrupt_task(task_id, payload)

    def complete_task(
        self,
        task_id: str,
        payload: RemoteTaskCompleteRequest,
    ) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.complete_task(task_id, payload)

    def fail_task(self, task_id: str, payload: RemoteTaskFailRequest) -> RemoteTaskSummaryResponse:
        return self.remote_task_service.fail_task(task_id, payload)
