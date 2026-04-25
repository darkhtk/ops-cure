"""HTTP surface for the kernel-level KernelTaskService."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_bridge_token
from ..db import session_scope
from ..kernel.tasks import KernelTaskRecord, TaskLeaseError

router = APIRouter(
    prefix="/api/kernel/tasks",
    tags=["kernel-tasks"],
    dependencies=[Depends(require_bridge_token)],
)


class TaskEnvelope(BaseModel):
    id: str
    space_id: str
    kind: str
    status: str
    priority: int
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = ""
    owner_actor_id: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    claim_count: int = 0
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    parent_task_id: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskClaimEnvelope(BaseModel):
    task: TaskEnvelope
    lease_token: str
    lease_expires_at: datetime


class TaskListResponse(BaseModel):
    tasks: list[TaskEnvelope]


class EnqueueBody(BaseModel):
    space_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    requested_by: str = ""
    parent_task_id: str | None = None


class ClaimNextBody(BaseModel):
    actor_id: str = Field(min_length=1)
    lease_seconds: int = Field(default=60, gt=0)
    space_id: str | None = None
    kinds: list[str] | None = None


class HeartbeatBody(BaseModel):
    lease_token: str = Field(min_length=1)
    lease_seconds: int = Field(default=60, gt=0)
    status: str = Field(default="executing")


class CompleteBody(BaseModel):
    lease_token: str = Field(min_length=1)
    result: dict[str, Any] | None = None


class FailBody(BaseModel):
    lease_token: str = Field(min_length=1)
    error: dict[str, Any] | None = None


class CancelBody(BaseModel):
    reason: str | None = None


def _envelope(record: KernelTaskRecord) -> TaskEnvelope:
    return TaskEnvelope(
        id=record.id,
        space_id=record.space_id,
        kind=record.kind,
        status=record.status,
        priority=record.priority,
        payload=record.payload,
        requested_by=record.requested_by,
        owner_actor_id=record.owner_actor_id,
        lease_token=record.lease_token,
        lease_expires_at=record.lease_expires_at,
        claim_count=record.claim_count,
        result=record.result,
        error=record.error,
        parent_task_id=record.parent_task_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


@router.post("", response_model=TaskEnvelope)
async def enqueue_task(request: Request, body: EnqueueBody) -> TaskEnvelope:
    services = request.app.state.services
    service = services.kernel_task_service
    with session_scope() as db:
        record = service.enqueue(
            db,
            space_id=body.space_id,
            kind=body.kind,
            payload=body.payload,
            priority=body.priority,
            requested_by=body.requested_by,
            parent_task_id=body.parent_task_id,
        )
    return _envelope(record)


@router.post("/claim-next", response_model=TaskClaimEnvelope | None)
async def claim_next(request: Request, body: ClaimNextBody) -> TaskClaimEnvelope | None:
    services = request.app.state.services
    service = services.kernel_task_service
    with session_scope() as db:
        claim = service.claim_next(
            db,
            space_id=body.space_id,
            kinds=body.kinds,
            actor_id=body.actor_id,
            lease_seconds=body.lease_seconds,
        )
    if claim is None:
        return None
    return TaskClaimEnvelope(
        task=_envelope(claim.task),
        lease_token=claim.lease_token,
        lease_expires_at=claim.lease_expires_at,
    )


@router.get("/{task_id}", response_model=TaskEnvelope)
async def get_task(task_id: str, request: Request) -> TaskEnvelope:
    services = request.app.state.services
    service = services.kernel_task_service
    with session_scope() as db:
        record = service.get(db, task_id=task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return _envelope(record)


@router.post("/{task_id}/heartbeat", response_model=TaskEnvelope)
async def heartbeat(task_id: str, request: Request, body: HeartbeatBody) -> TaskEnvelope:
    services = request.app.state.services
    service = services.kernel_task_service
    try:
        with session_scope() as db:
            record = service.heartbeat(
                db,
                task_id=task_id,
                lease_token=body.lease_token,
                lease_seconds=body.lease_seconds,
                status=body.status,
            )
    except TaskLeaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _envelope(record)


@router.post("/{task_id}/complete", response_model=TaskEnvelope)
async def complete(task_id: str, request: Request, body: CompleteBody) -> TaskEnvelope:
    services = request.app.state.services
    service = services.kernel_task_service
    try:
        with session_scope() as db:
            record = service.complete(
                db,
                task_id=task_id,
                lease_token=body.lease_token,
                result=body.result,
            )
    except TaskLeaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _envelope(record)


@router.post("/{task_id}/fail", response_model=TaskEnvelope)
async def fail(task_id: str, request: Request, body: FailBody) -> TaskEnvelope:
    services = request.app.state.services
    service = services.kernel_task_service
    try:
        with session_scope() as db:
            record = service.fail(
                db,
                task_id=task_id,
                lease_token=body.lease_token,
                error=body.error,
            )
    except TaskLeaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _envelope(record)


@router.post("/{task_id}/cancel", response_model=TaskEnvelope)
async def cancel(task_id: str, request: Request, body: CancelBody) -> TaskEnvelope:
    services = request.app.state.services
    service = services.kernel_task_service
    with session_scope() as db:
        record = service.cancel(db, task_id=task_id, reason=body.reason)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return _envelope(record)


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    space_id: str | None = Query(default=None),
    kinds: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> TaskListResponse:
    services = request.app.state.services
    service = services.kernel_task_service
    with session_scope() as db:
        records = service.list(
            db,
            space_id=space_id,
            kinds=kinds,
            statuses=statuses,
            limit=limit,
        )
    return TaskListResponse(tasks=[_envelope(record) for record in records])
