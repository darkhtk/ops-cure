"""HTTP surface for the kernel-level KernelScratchService.

Endpoints are deliberately simple — get / set / delete on a single
``(actor_id, space_id, key)`` triple, plus a shallow list-by-scope so
behaviors that need to enumerate keys by actor or space don't have to
keep their own index. Auth uses the existing bridge shared token so the
endpoint is reachable from any pc_launcher connector that already speaks
to the bridge.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import require_bridge_token
from ..db import session_scope

router = APIRouter(
    prefix="/api/kernel/scratch",
    tags=["kernel-scratch"],
    dependencies=[Depends(require_bridge_token)],
)


class ScratchEntry(BaseModel):
    actor_id: str = ""
    space_id: str = ""
    key: str
    value: Any = None
    expires_at: str | None = None


class ScratchSetRequest(BaseModel):
    actor_id: str = ""
    space_id: str = ""
    key: str = Field(min_length=1)
    value: Any = None
    ttl_seconds: int | None = Field(default=None)


class ScratchSetResponse(BaseModel):
    ok: bool = True


class ScratchDeleteRequest(BaseModel):
    actor_id: str = ""
    space_id: str = ""
    key: str = Field(min_length=1)


class ScratchDeleteResponse(BaseModel):
    ok: bool
    removed: bool


class ScratchValueResponse(BaseModel):
    found: bool
    value: Any = None


@router.get("", response_model=ScratchValueResponse)
async def get_scratch(
    request: Request,
    key: str = Query(min_length=1),
    actor_id: str = Query(default=""),
    space_id: str = Query(default=""),
) -> ScratchValueResponse:
    services = request.app.state.services
    scratch = services.kernel_scratch_service
    sentinel = object()
    with session_scope() as db:
        value = scratch.get(
            db,
            key=key,
            actor_id=actor_id,
            space_id=space_id,
            default=sentinel,
        )
    if value is sentinel:
        return ScratchValueResponse(found=False, value=None)
    return ScratchValueResponse(found=True, value=value)


@router.put("", response_model=ScratchSetResponse)
async def set_scratch(request: Request, body: ScratchSetRequest) -> ScratchSetResponse:
    services = request.app.state.services
    scratch = services.kernel_scratch_service
    with session_scope() as db:
        scratch.set(
            db,
            key=body.key,
            value=body.value,
            actor_id=body.actor_id,
            space_id=body.space_id,
            ttl_seconds=body.ttl_seconds,
        )
    return ScratchSetResponse(ok=True)


@router.delete("", response_model=ScratchDeleteResponse)
async def delete_scratch(request: Request, body: ScratchDeleteRequest) -> ScratchDeleteResponse:
    services = request.app.state.services
    scratch = services.kernel_scratch_service
    with session_scope() as db:
        removed = scratch.delete(
            db,
            key=body.key,
            actor_id=body.actor_id,
            space_id=body.space_id,
        )
    return ScratchDeleteResponse(ok=True, removed=removed)
