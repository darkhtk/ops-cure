from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import require_bridge_token
from ..kernel.presence import (
    ActorSessionSummary,
    ActorSessionUpsertRequest,
    ResourceLeaseClaimRequest,
    ResourceLeaseHeartbeatRequest,
    ResourceLeaseReleaseRequest,
    ResourceLeaseSummary,
    ScopePresenceResponse,
)

router = APIRouter(
    prefix="/api",
    tags=["presence"],
    dependencies=[Depends(require_bridge_token)],
)


@router.post("/presence/sessions", response_model=ActorSessionSummary)
async def upsert_actor_session(payload: ActorSessionUpsertRequest, request: Request) -> ActorSessionSummary:
    return request.app.state.services.presence_service.upsert_actor_session(payload)


@router.get("/presence/scopes/{scope_kind}/{scope_id}", response_model=ScopePresenceResponse)
async def get_scope_presence(
    scope_kind: str,
    scope_id: str,
    request: Request,
    active_only: bool = Query(default=True),
) -> ScopePresenceResponse:
    return request.app.state.services.presence_service.list_presence(
        scope_kind=scope_kind,
        scope_id=scope_id,
        active_only=active_only,
    )


@router.post("/leases", response_model=ResourceLeaseSummary)
async def claim_resource_lease(payload: ResourceLeaseClaimRequest, request: Request) -> ResourceLeaseSummary:
    try:
        return request.app.state.services.presence_service.claim_resource_lease(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/leases/resources/{resource_kind}/{resource_id}", response_model=ResourceLeaseSummary | None)
async def get_current_resource_lease(resource_kind: str, resource_id: str, request: Request) -> ResourceLeaseSummary | None:
    return request.app.state.services.presence_service.get_current_lease(
        resource_kind=resource_kind,
        resource_id=resource_id,
    )


@router.post("/leases/{lease_id}/heartbeat", response_model=ResourceLeaseSummary)
async def heartbeat_resource_lease(
    lease_id: str,
    payload: ResourceLeaseHeartbeatRequest,
    request: Request,
) -> ResourceLeaseSummary:
    try:
        return request.app.state.services.presence_service.heartbeat_resource_lease(
            lease_id=lease_id,
            payload=payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/leases/{lease_id}/release", response_model=ResourceLeaseSummary)
async def release_resource_lease(
    lease_id: str,
    payload: ResourceLeaseReleaseRequest,
    request: Request,
) -> ResourceLeaseSummary:
    try:
        return request.app.state.services.presence_service.release_resource_lease(
            lease_id=lease_id,
            payload=payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
