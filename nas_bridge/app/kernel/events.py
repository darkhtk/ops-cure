"""Generic event vocabulary across behaviors."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from .contracts import EventProvider


class EventSummary(BaseModel):
    id: str
    kind: str
    actor_name: str
    content: str
    created_at: datetime


class EventListResponse(BaseModel):
    space_id: str
    domain_type: str
    events: list[EventSummary] = Field(default_factory=list)


class EventService:
    def __init__(self, *, providers: Iterable[EventProvider] = ()) -> None:
        self.providers = tuple(providers)

    def get_events_for_space(self, *, space_id: str, limit: int = 20) -> EventListResponse | None:
        for provider in self.providers:
            response = provider.get_events_for_space(space_id=space_id, limit=limit)
            if response is not None:
                return response
        return None

    def get_events_for_thread(self, *, thread_id: str, limit: int = 20) -> EventListResponse | None:
        for provider in self.providers:
            response = provider.get_events_for_thread(thread_id=thread_id, limit=limit)
            if response is not None:
                return response
        return None
