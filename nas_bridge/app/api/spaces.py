from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_bridge_token
from ..kernel.spaces import SpaceSummary

router = APIRouter(
    prefix="/api/spaces",
    tags=["spaces"],
    dependencies=[Depends(require_bridge_token)],
)


@router.get("/{space_id}", response_model=SpaceSummary)
async def get_space(space_id: str, request: Request) -> SpaceSummary:
    services = request.app.state.services
    summary = services.space_service.get_space(space_id=space_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return summary


@router.get("/by-thread/{thread_id}", response_model=SpaceSummary)
async def get_space_by_thread(thread_id: str, request: Request) -> SpaceSummary:
    services = request.app.state.services
    summary = services.space_service.get_space_by_thread(thread_id=thread_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return summary
