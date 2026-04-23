"""Behavior-level API facade for remote_codex."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ...auth import require_bridge_token

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

router = APIRouter(
    prefix="/api/remote-codex",
    tags=["remote_codex"],
    dependencies=[Depends(require_bridge_token)],
)


@router.post("/tasks", response_model=RemoteTaskSummaryResponse)
async def create_task(payload: RemoteTaskCreateRequest, request: Request) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.create_task(payload)


@router.get("/tasks/{task_id}", response_model=RemoteTaskSummaryResponse)
async def get_task(task_id: str, request: Request) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.get_task(task_id)


@router.get("/machines/{machine_id}/tasks", response_model=list[RemoteTaskSummaryResponse])
async def list_machine_tasks(
    machine_id: str,
    request: Request,
    statuses: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[RemoteTaskSummaryResponse]:
    return request.app.state.services.remote_codex_service.list_machine_tasks(
        machine_id=machine_id,
        statuses=statuses,
        limit=limit,
    )


@router.post("/machines/{machine_id}/tasks/claim-next", response_model=RemoteTaskSummaryResponse | None)
async def claim_next_machine_task(
    machine_id: str,
    payload: RemoteTaskClaimNextRequest,
    request: Request,
) -> RemoteTaskSummaryResponse | None:
    return request.app.state.services.remote_codex_service.claim_next_machine_task(
        machine_id=machine_id,
        payload=payload,
    )


@router.get("/threads/{thread_id}/tasks", response_model=list[RemoteTaskSummaryResponse])
async def list_thread_tasks(
    thread_id: str,
    request: Request,
    statuses: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[RemoteTaskSummaryResponse]:
    return request.app.state.services.remote_codex_service.list_thread_tasks(
        thread_id=thread_id,
        statuses=statuses,
        limit=limit,
    )


@router.post("/tasks/{task_id}/claim", response_model=RemoteTaskSummaryResponse)
async def claim_task(
    task_id: str,
    payload: RemoteTaskClaimRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.claim_task(task_id, payload)


@router.post("/tasks/{task_id}/heartbeat", response_model=RemoteTaskSummaryResponse)
async def heartbeat_task(
    task_id: str,
    payload: RemoteTaskHeartbeatRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.heartbeat_task(task_id, payload)


@router.post("/tasks/{task_id}/evidence", response_model=RemoteTaskSummaryResponse)
async def add_evidence(
    task_id: str,
    payload: RemoteTaskEvidenceRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.add_evidence(task_id, payload)


@router.get("/tasks/{task_id}/approval", response_model=RemoteTaskApprovalSummary | None)
async def get_approval(task_id: str, request: Request) -> RemoteTaskApprovalSummary | None:
    return request.app.state.services.remote_codex_service.get_latest_approval(task_id)


@router.post("/tasks/{task_id}/approval", response_model=RemoteTaskSummaryResponse)
async def request_approval(
    task_id: str,
    payload: RemoteTaskApprovalRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.request_approval(task_id, payload)


@router.post("/tasks/{task_id}/approval/resolve", response_model=RemoteTaskSummaryResponse)
async def resolve_approval(
    task_id: str,
    payload: RemoteTaskApprovalResolveRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.resolve_approval(task_id, payload)


@router.get("/tasks/{task_id}/notes", response_model=list[RemoteTaskNoteSummary])
async def list_notes(task_id: str, request: Request) -> list[RemoteTaskNoteSummary]:
    return request.app.state.services.remote_codex_service.list_notes(task_id)


@router.post("/tasks/{task_id}/notes", response_model=RemoteTaskNoteSummary)
async def add_note(
    task_id: str,
    payload: RemoteTaskNoteRequest,
    request: Request,
) -> RemoteTaskNoteSummary:
    return request.app.state.services.remote_codex_service.add_note(task_id, payload)


@router.post("/tasks/{task_id}/interrupt", response_model=RemoteTaskSummaryResponse)
async def interrupt_task(
    task_id: str,
    payload: RemoteTaskInterruptRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.interrupt_task(task_id, payload)


@router.post("/tasks/{task_id}/complete", response_model=RemoteTaskSummaryResponse)
async def complete_task(
    task_id: str,
    payload: RemoteTaskCompleteRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.complete_task(task_id, payload)


@router.post("/tasks/{task_id}/fail", response_model=RemoteTaskSummaryResponse)
async def fail_task(
    task_id: str,
    payload: RemoteTaskFailRequest,
    request: Request,
) -> RemoteTaskSummaryResponse:
    return request.app.state.services.remote_codex_service.fail_task(task_id, payload)
