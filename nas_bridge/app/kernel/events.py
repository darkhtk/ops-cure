"""Generic event vocabulary and delta contracts across behaviors."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .contracts import EventProvider


def encode_event_cursor(*, created_at: datetime, event_id: str) -> str:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)
    micros = int(created_at.timestamp() * 1_000_000)
    return f"{micros:020d}:{event_id}"


def is_valid_event_cursor(cursor: str | None) -> bool:
    if not cursor:
        return True
    prefix, separator, suffix = cursor.partition(":")
    return bool(separator and prefix.isdigit() and suffix)


class EventCursor(BaseModel):
    value: str


class EventSummary(BaseModel):
    id: str
    kind: str
    actor_name: str
    content: str
    created_at: datetime


class EventEnvelope(BaseModel):
    cursor: str
    space_id: str
    event: EventSummary


class EventDeltaResponse(BaseModel):
    space_id: str
    domain_type: str
    items: list[EventEnvelope] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False

    @property
    def events(self) -> list[EventSummary]:
        return [item.event for item in self.items]


class EventStreamOpenResponse(BaseModel):
    space_id: str
    accepted_after_cursor: str | None = None
    latest_cursor: str | None = None


class EventStreamResetResponse(BaseModel):
    space_id: str
    reason: str


class SubscriptionPresenceSummary(BaseModel):
    subscriber_id: str
    space_id: str
    last_seen_at: datetime
    expires_at: datetime | None = None


class EventService:
    def __init__(self, *, providers: Iterable[EventProvider] = ()) -> None:
        self.providers = tuple(providers)

    def get_events_for_space(
        self,
        *,
        space_id: str,
        after_cursor: str | None = None,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> EventDeltaResponse | None:
        for provider in self.providers:
            response = provider.get_events_for_space(
                space_id=space_id,
                after_cursor=after_cursor,
                limit=limit,
                kinds=kinds,
            )
            if response is not None:
                return response
        return None

    def get_events_for_thread(
        self,
        *,
        thread_id: str,
        after_cursor: str | None = None,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> EventDeltaResponse | None:
        for provider in self.providers:
            response = provider.get_events_for_thread(
                thread_id=thread_id,
                after_cursor=after_cursor,
                limit=limit,
                kinds=kinds,
            )
            if response is not None:
                return response
        return None


def paginate_event_envelopes(
    *,
    space_id: str,
    domain_type: str,
    items: Iterable[EventEnvelope],
    after_cursor: str | None = None,
    limit: int = 20,
    kinds: list[str] | None = None,
) -> EventDeltaResponse:
    """Return a stable old-to-new event page for both snapshot and resume reads."""
    filtered_items = list(items)
    if kinds:
        allowed = set(kinds)
        filtered_items = [item for item in filtered_items if item.event.kind in allowed]

    if after_cursor is None:
        page = filtered_items[-limit:] if limit > 0 else filtered_items
        has_more = len(filtered_items) > len(page)
    else:
        unread = [item for item in filtered_items if item.cursor > after_cursor]
        page = unread[:limit]
        has_more = len(unread) > limit
    next_cursor = page[-1].cursor if page else after_cursor
    return EventDeltaResponse(
        space_id=space_id,
        domain_type=domain_type,
        items=page,
        next_cursor=next_cursor,
        has_more=has_more,
    )
