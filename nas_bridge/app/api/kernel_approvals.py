"""HTTP surface for the kernel-level KernelApprovalService.

Behaviors that need approvals can either inject KernelApprovalService
directly (in-process) or call this endpoint over HTTP from a runner /
external connector. Auth uses the existing bridge shared token so any
pc_launcher connector that already speaks to the bridge can request and
resolve approvals without learning a new protocol.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_bridge_token
from ..db import session_scope
from ..kernel.approvals import ApprovalRecord

router = APIRouter(
    prefix="/api/kernel/approvals",
    tags=["kernel-approvals"],
    dependencies=[Depends(require_bridge_token)],
)


class ApprovalEnvelope(BaseModel):
    id: str
    space_id: str
    kind: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = ""
    requested_at: datetime
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None
    note: str | None = None
    expires_at: datetime | None = None


def _envelope_from_record(record: ApprovalRecord) -> ApprovalEnvelope:
    return ApprovalEnvelope(
        id=record.id,
        space_id=record.space_id,
        kind=record.kind,
        status=record.status,
        payload=record.payload,
        requested_by=record.requested_by,
        requested_at=record.requested_at,
        resolved_by=record.resolved_by,
        resolved_at=record.resolved_at,
        resolution=record.resolution,
        note=record.note,
        expires_at=record.expires_at,
    )


class ApprovalRequestBody(BaseModel):
    space_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = ""
    ttl_seconds: int | None = None


class ApprovalResolveBody(BaseModel):
    resolution: str = Field(min_length=1)
    resolved_by: str = ""
    note: str | None = None


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalEnvelope]


@router.post("", response_model=ApprovalEnvelope)
async def request_approval(request: Request, body: ApprovalRequestBody) -> ApprovalEnvelope:
    services = request.app.state.services
    service = services.kernel_approval_service
    with session_scope() as db:
        record = service.request(
            db,
            space_id=body.space_id,
            kind=body.kind,
            payload=body.payload,
            requested_by=body.requested_by,
            ttl_seconds=body.ttl_seconds,
        )
    return _envelope_from_record(record)


@router.get("/{approval_id}", response_model=ApprovalEnvelope)
async def get_approval(approval_id: str, request: Request) -> ApprovalEnvelope:
    services = request.app.state.services
    service = services.kernel_approval_service
    with session_scope() as db:
        record = service.get(db, approval_id=approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Approval not found.")
    return _envelope_from_record(record)


@router.post("/{approval_id}/resolve", response_model=ApprovalEnvelope)
async def resolve_approval(
    approval_id: str,
    request: Request,
    body: ApprovalResolveBody,
) -> ApprovalEnvelope:
    services = request.app.state.services
    service = services.kernel_approval_service
    with session_scope() as db:
        record = service.resolve(
            db,
            approval_id=approval_id,
            resolution=body.resolution,
            resolved_by=body.resolved_by,
            note=body.note,
        )
    if record is None:
        raise HTTPException(status_code=404, detail="Approval not found.")
    return _envelope_from_record(record)


@router.get("", response_model=ApprovalListResponse)
async def list_pending_approvals(
    request: Request,
    space_id: str = Query(min_length=1),
    kinds: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> ApprovalListResponse:
    services = request.app.state.services
    service = services.kernel_approval_service
    with session_scope() as db:
        records = service.list_pending(
            db,
            space_id=space_id,
            kinds=kinds,
            limit=limit,
        )
    return ApprovalListResponse(
        approvals=[_envelope_from_record(record) for record in records],
    )
