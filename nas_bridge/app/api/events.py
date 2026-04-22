from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth import require_bridge_token
from ..kernel.events import EventListResponse

router = APIRouter(
    prefix="/api/events",
    tags=["events"],
    dependencies=[Depends(require_bridge_token)],
)


@router.get("/spaces/{space_id}", response_model=EventListResponse)
async def get_events_for_space(
    space_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> EventListResponse:
    services = request.app.state.services
    response = services.event_service.get_events_for_space(space_id=space_id, limit=limit)
    if response is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return response


@router.get("/threads/{thread_id}", response_model=EventListResponse)
async def get_events_for_thread(
    thread_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> EventListResponse:
    services = request.app.state.services
    response = services.event_service.get_events_for_thread(thread_id=thread_id, limit=limit)
    if response is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return response
