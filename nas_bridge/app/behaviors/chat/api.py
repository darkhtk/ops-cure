from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...auth import require_bridge_token
from .conversation_schemas import (
    ChatRoomHealthResponse,
    ChatTaskClaimRequest,
    ChatTaskCompleteRequest,
    ChatTaskEvidenceRequest,
    ChatTaskFailRequest,
    ChatTaskHeartbeatRequest,
    ChatTaskStateResponse,
    ConversationCloseRequest,
    ConversationDetailResponse,
    ConversationHandoffRequest,
    ConversationListResponse,
    ConversationOpenRequest,
    ConversationSummary,
    IdleSweepResponse,
    SpeechActSubmitRequest,
    SpeechActSummary,
)
from .conversation_service import (
    ChatConversationNotFoundError,
    ChatConversationStateError,
    ChatThreadNotFoundError,
)
from .task_coordinator import ChatTaskBindingError
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
    response = await services.chat_service.submit_participant_message_and_notify(
        thread_id=thread_id,
        actor_name=payload.actor_name,
        actor_kind=payload.actor_kind,
        content=payload.content,
    )
    if response is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return response


# ---- conversation protocol layer -------------------------------------------


@router.post(
    "/threads/{thread_id}/conversations",
    response_model=ConversationSummary,
    status_code=201,
)
async def open_conversation(
    thread_id: str,
    payload: ConversationOpenRequest,
    request: Request,
) -> ConversationSummary:
    services = request.app.state.services
    try:
        return services.chat_conversation_service.open_conversation(
            discord_thread_id=thread_id,
            request=payload,
        )
    except ChatThreadNotFoundError:
        raise HTTPException(status_code=404, detail="Chat thread not found.")


@router.get(
    "/threads/{thread_id}/conversations",
    response_model=ConversationListResponse,
)
async def list_conversations(
    thread_id: str,
    request: Request,
    state: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    include_general: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
) -> ConversationListResponse:
    services = request.app.state.services
    try:
        return services.chat_conversation_service.list_conversations(
            discord_thread_id=thread_id,
            state=state,
            kind=kind,
            include_general=include_general,
            limit=limit,
        )
    except ChatThreadNotFoundError:
        raise HTTPException(status_code=404, detail="Chat thread not found.")


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_conversation(
    conversation_id: str,
    request: Request,
    recent: int = Query(default=30, ge=1, le=200),
    kinds: list[str] | None = Query(default=None),
) -> ConversationDetailResponse:
    services = request.app.state.services
    try:
        return services.chat_conversation_service.get_conversation(
            conversation_id=conversation_id,
            recent=recent,
            kinds=kinds,
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found.")


@router.post(
    "/conversations/{conversation_id}/speech",
    response_model=SpeechActSummary,
    status_code=201,
)
async def submit_speech(
    conversation_id: str,
    payload: SpeechActSubmitRequest,
    request: Request,
) -> SpeechActSummary:
    services = request.app.state.services
    try:
        return services.chat_conversation_service.submit_speech(
            conversation_id=conversation_id,
            request=payload,
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/conversations/{conversation_id}/close",
    response_model=ConversationSummary,
)
async def close_conversation(
    conversation_id: str,
    payload: ConversationCloseRequest,
    request: Request,
) -> ConversationSummary:
    services = request.app.state.services
    try:
        return services.chat_conversation_service.close_conversation(
            conversation_id=conversation_id,
            closed_by=payload.closed_by,
            resolution=payload.resolution,
            summary=payload.summary,
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---- handoff & idle sweep --------------------------------------------------


@router.post(
    "/conversations/{conversation_id}/handoff",
    response_model=ConversationSummary,
)
async def handoff_conversation(
    conversation_id: str,
    payload: ConversationHandoffRequest,
    request: Request,
) -> ConversationSummary:
    services = request.app.state.services
    try:
        return services.chat_conversation_service.transfer_owner(
            conversation_id=conversation_id,
            by_actor=payload.by_actor,
            new_owner=payload.new_owner,
            reason=payload.reason,
        )
    except ChatConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    except ChatConversationStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get(
    "/threads/{thread_id}/health",
    response_model=ChatRoomHealthResponse,
)
async def get_room_health(
    thread_id: str,
    request: Request,
    idle_threshold_seconds: int = Query(default=30 * 60, ge=60, le=86_400),
) -> ChatRoomHealthResponse:
    services = request.app.state.services
    try:
        snapshot = services.chat_conversation_service.get_room_health(
            discord_thread_id=thread_id,
            idle_threshold_seconds=idle_threshold_seconds,
        )
    except ChatThreadNotFoundError:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return ChatRoomHealthResponse(**snapshot)


@router.post(
    "/threads/{thread_id}/sweep-idle",
    response_model=IdleSweepResponse,
)
async def sweep_idle_conversations(
    thread_id: str,
    request: Request,
    idle_threshold_seconds: int = Query(default=1800, ge=0, le=86_400),
) -> IdleSweepResponse:
    services = request.app.state.services
    try:
        flagged = services.chat_conversation_service.sweep_idle_conversations(
            discord_thread_id=thread_id,
            idle_threshold_seconds=idle_threshold_seconds,
        )
    except ChatThreadNotFoundError:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return IdleSweepResponse(
        thread_id=thread_id,
        idle_threshold_seconds=idle_threshold_seconds,
        flagged=flagged,
    )


# ---- task lifecycle (kind=task only) ---------------------------------------


@router.post(
    "/conversations/{conversation_id}/task/claim",
    response_model=ChatTaskStateResponse,
)
async def claim_task_conversation(
    conversation_id: str,
    payload: ChatTaskClaimRequest,
    request: Request,
) -> ChatTaskStateResponse:
    services = request.app.state.services
    try:
        return services.chat_task_coordinator.claim(
            conversation_id=conversation_id,
            request=payload,
        )
    except ChatTaskBindingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/conversations/{conversation_id}/task/heartbeat",
    response_model=ChatTaskStateResponse,
)
async def heartbeat_task_conversation(
    conversation_id: str,
    payload: ChatTaskHeartbeatRequest,
    request: Request,
) -> ChatTaskStateResponse:
    services = request.app.state.services
    try:
        return services.chat_task_coordinator.heartbeat(
            conversation_id=conversation_id,
            request=payload,
        )
    except ChatTaskBindingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/conversations/{conversation_id}/task/evidence",
    response_model=ChatTaskStateResponse,
)
async def evidence_task_conversation(
    conversation_id: str,
    payload: ChatTaskEvidenceRequest,
    request: Request,
) -> ChatTaskStateResponse:
    services = request.app.state.services
    try:
        return services.chat_task_coordinator.add_evidence(
            conversation_id=conversation_id,
            request=payload,
        )
    except ChatTaskBindingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/conversations/{conversation_id}/task/complete",
    response_model=ChatTaskStateResponse,
)
async def complete_task_conversation(
    conversation_id: str,
    payload: ChatTaskCompleteRequest,
    request: Request,
) -> ChatTaskStateResponse:
    services = request.app.state.services
    try:
        return services.chat_task_coordinator.complete(
            conversation_id=conversation_id,
            request=payload,
        )
    except ChatTaskBindingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/conversations/{conversation_id}/task/fail",
    response_model=ChatTaskStateResponse,
)
async def fail_task_conversation(
    conversation_id: str,
    payload: ChatTaskFailRequest,
    request: Request,
) -> ChatTaskStateResponse:
    services = request.app.state.services
    try:
        return services.chat_task_coordinator.fail(
            conversation_id=conversation_id,
            request=payload,
        )
    except ChatTaskBindingError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
