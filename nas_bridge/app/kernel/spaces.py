"""Generic space vocabulary layered above behavior-specific state."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, Field

from .contracts import SpaceProvider


class ActorSummary(BaseModel):
    name: str
    kind: str
    status: str
    detail: str | None = None


class SpaceSummary(BaseModel):
    id: str
    domain_type: str
    transport_kind: str
    transport_address: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime | None = None
    actors: list[ActorSummary] = Field(default_factory=list)
    metadata: dict[str, str | int | bool | None] = Field(default_factory=dict)


class SpaceService:
    """Builds generic space summaries across multiple behaviors."""

    def __init__(self, *, providers: Iterable[SpaceProvider] = ()) -> None:
        self.providers = tuple(providers)

    def get_space_by_thread(self, *, thread_id: str) -> SpaceSummary | None:
        for provider in self.providers:
            summary = provider.get_space_by_thread(thread_id=thread_id)
            if summary is not None:
                return summary
        return None

    def get_space(self, *, space_id: str) -> SpaceSummary | None:
        for provider in self.providers:
            summary = provider.get_space(space_id=space_id)
            if summary is not None:
                return summary
        return None
