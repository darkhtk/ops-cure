"""Generic actor vocabulary across behaviors."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from .contracts import ActorProvider


class ActorSummary(BaseModel):
    name: str
    kind: str
    status: str
    detail: str | None = None
    turns: int | None = None
    last_active_at: datetime | None = None


class ActorListResponse(BaseModel):
    space_id: str
    domain_type: str
    actors: list[ActorSummary] = Field(default_factory=list)


class ActorService:
    def __init__(self, *, providers: Iterable[ActorProvider] = ()) -> None:
        self.providers = tuple(providers)

    def get_actors_for_space(self, *, space_id: str) -> ActorListResponse | None:
        for provider in self.providers:
            response = provider.get_actors_for_space(space_id=space_id)
            if response is not None:
                return response
        return None

    def get_actors_for_thread(self, *, thread_id: str) -> ActorListResponse | None:
        for provider in self.providers:
            response = provider.get_actors_for_thread(thread_id=thread_id)
            if response is not None:
                return response
        return None
