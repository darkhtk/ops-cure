from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..auth import require_bridge_token
from ..kernel.events import (
    EventDeltaResponse,
    EventEnvelope,
    EventStreamOpenResponse,
    EventStreamResetResponse,
    is_valid_event_cursor,
)

router = APIRouter(
    prefix="/api/events",
    tags=["events"],
    dependencies=[Depends(require_bridge_token)],
)


def _encode_sse(event_name: str, payload: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _require_valid_cursor(after_cursor: str | None) -> None:
    if not is_valid_event_cursor(after_cursor):
        raise HTTPException(status_code=400, detail="Invalid event cursor.")


async def _stream_space_events(
    *,
    services,
    space_id: str,
    after_cursor: str | None,
    limit: int,
    kinds: list[str] | None,
    subscriber_id: str | None,
) -> AsyncIterator[str]:
    if after_cursor is not None and not is_valid_event_cursor(after_cursor):
        reset = EventStreamResetResponse(space_id=space_id, reason="invalid_cursor")
        yield _encode_sse("reset", reset.model_dump(mode="json"))
        return

    subscription = services.subscription_broker.subscribe(
        space_id=space_id,
        after_cursor=after_cursor,
        kinds=kinds,
        subscriber_id=subscriber_id,
    )
    if subscription.reset_reason is not None:
        reset = EventStreamResetResponse(space_id=space_id, reason=subscription.reset_reason)
        subscription.close()
        yield _encode_sse("reset", reset.model_dump(mode="json"))
        return

    latest_cursor = subscription.latest_cursor
    if latest_cursor is None:
        latest = services.event_service.get_events_for_space(space_id=space_id, limit=1)
        latest_cursor = latest.next_cursor if latest is not None else None
    open_payload = EventStreamOpenResponse(
        space_id=space_id,
        accepted_after_cursor=subscription.accepted_after_cursor,
        latest_cursor=latest_cursor,
    )
    yield _encode_sse("open", open_payload.model_dump(mode="json"))

    cursor = after_cursor
    try:
        while True:
            next_item = await subscription.next_event(timeout_seconds=15.0)
            if next_item is None:
                heartbeat = {"space_id": space_id, "cursor": cursor}
                yield _encode_sse("heartbeat", heartbeat)
                continue
            cursor = next_item.cursor
            yield _encode_sse("event", next_item.model_dump(mode="json"))
    finally:
        subscription.close()


@router.get("/spaces/{space_id}", response_model=EventDeltaResponse)
async def get_events_for_space(
    space_id: str,
    request: Request,
    after_cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    kinds: list[str] | None = Query(default=None),
) -> EventDeltaResponse:
    _require_valid_cursor(after_cursor)
    services = request.app.state.services
    response = services.event_service.get_events_for_space(
        space_id=space_id,
        after_cursor=after_cursor,
        limit=limit,
        kinds=kinds,
    )
    if response is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return response


@router.get("/threads/{thread_id}", response_model=EventDeltaResponse)
async def get_events_for_thread(
    thread_id: str,
    request: Request,
    after_cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    kinds: list[str] | None = Query(default=None),
) -> EventDeltaResponse:
    _require_valid_cursor(after_cursor)
    services = request.app.state.services
    response = services.event_service.get_events_for_thread(
        thread_id=thread_id,
        after_cursor=after_cursor,
        limit=limit,
        kinds=kinds,
    )
    if response is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    return response


@router.get("/spaces/{space_id}/stream")
async def stream_events_for_space(
    space_id: str,
    request: Request,
    after_cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    kinds: list[str] | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
) -> StreamingResponse:
    services = request.app.state.services
    if services.space_service.get_space(space_id=space_id) is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    generator = _stream_space_events(
        services=services,
        space_id=space_id,
        after_cursor=after_cursor,
        limit=limit,
        kinds=kinds,
        subscriber_id=subscriber_id,
    )
    return StreamingResponse(generator, media_type="text/event-stream")


@router.get("/threads/{thread_id}/stream")
async def stream_events_for_thread(
    thread_id: str,
    request: Request,
    after_cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    kinds: list[str] | None = Query(default=None),
    subscriber_id: str | None = Query(default=None),
) -> StreamingResponse:
    services = request.app.state.services
    space = services.space_service.get_space_by_thread(thread_id=thread_id)
    if space is None:
        raise HTTPException(status_code=404, detail="Space not found.")
    generator = _stream_space_events(
        services=services,
        space_id=space.id,
        after_cursor=after_cursor,
        limit=limit,
        kinds=kinds,
        subscriber_id=subscriber_id,
    )
    return StreamingResponse(generator, media_type="text/event-stream")
