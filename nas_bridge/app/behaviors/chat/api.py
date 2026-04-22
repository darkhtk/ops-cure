from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...auth import require_bridge_token
from .schemas import (
    ChatMessageSubmitRequest,
    ChatMessageSubmitResponse,
    ChatParticipantHeartbeatRequest,
    ChatParticipantRegisterRequest,
    ChatParticipantSummary,
    ChatThreadDeltaResponse,
)

router = APIRouter(
    prefix="/api/chat",
    tags=["chat"],
    dependencies=[Depends(require_bridge_token)],
)


@router.post("/threads/{thread_id}/participants/register", response_model=ChatParticipantSummary)
async def register_chat_participant(
    thread_id: str,
    payload: ChatParticipantRegisterRequest,
    request: Request,
) -> ChatParticipantSummary:
    services = request.app.state.services
    summary = services.chat_service.register_participant(
        thread_id=thread_id,
        actor_name=payload.actor_name,
        actor_kind=payload.actor_kind,
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return summary


@router.post("/threads/{thread_id}/participants/heartbeat", response_model=ChatParticipantSummary)
async def heartbeat_chat_participant(
    thread_id: str,
    payload: ChatParticipantHeartbeatRequest,
    request: Request,
) -> ChatParticipantSummary:
    services = request.app.state.services
    summary = services.chat_service.heartbeat_participant(
        thread_id=thread_id,
        actor_name=payload.actor_name,
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return summary


@router.get("/threads/{thread_id}/delta", response_model=ChatThreadDeltaResponse)
async def get_chat_thread_delta(
    thread_id: str,
    request: Request,
    actor_name: str = Query(..., min_length=1),
    after_message_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    mark_read: bool = Query(default=False),
) -> ChatThreadDeltaResponse:
    services = request.app.state.services
    response = services.chat_service.get_thread_delta(
        thread_id=thread_id,
        actor_name=actor_name,
        after_message_id=after_message_id,
        limit=limit,
        mark_read=mark_read,
    )
    if response is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return response


@router.post("/threads/{thread_id}/messages", response_model=ChatMessageSubmitResponse)
async def submit_chat_message(
    thread_id: str,
    payload: ChatMessageSubmitRequest,
    request: Request,
) -> ChatMessageSubmitResponse:
    services = request.app.state.services
    response = services.chat_service.submit_participant_message(
        thread_id=thread_id,
        actor_name=payload.actor_name,
        actor_kind=payload.actor_kind,
        content=payload.content,
    )
    if response is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return response
