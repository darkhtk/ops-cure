from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_bridge_token
from ..kernel.actors import ActorListResponse

router = APIRouter(
    prefix="/api/actors",
    tags=["actors"],
    dependencies=[Depends(require_bridge_token)],
)


@router.get("/spaces/{space_id}", response_model=ActorListResponse)
async def get_actors_for_space(space_id: str, request: Request) -> ActorListResponse:
    services = request.app.state.services
    response = services.actor_service.get_actors_for_space(space_id=space_id)
    if response is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return response


@router.get("/threads/{thread_id}", response_model=ActorListResponse)
async def get_actors_for_thread(thread_id: str, request: Request) -> ActorListResponse:
    services = request.app.state.services
    response = services.actor_service.get_actors_for_thread(thread_id=thread_id)
    if response is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return response
