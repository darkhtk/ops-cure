"""Kernel provider contracts for behavior plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .actors import ActorListResponse
    from .events import EventListResponse
    from .spaces import SpaceSummary


class SpaceProvider(Protocol):
    behavior_id: str

    def get_space(self, *, space_id: str) -> "SpaceSummary | None":
        ...

    def get_space_by_thread(self, *, thread_id: str) -> "SpaceSummary | None":
        ...


class ActorProvider(Protocol):
    behavior_id: str

    def get_actors_for_space(self, *, space_id: str) -> "ActorListResponse | None":
        ...

    def get_actors_for_thread(self, *, thread_id: str) -> "ActorListResponse | None":
        ...


class EventProvider(Protocol):
    behavior_id: str

    def get_events_for_space(self, *, space_id: str, limit: int = 20) -> "EventListResponse | None":
        ...

    def get_events_for_thread(
        self,
        *,
        thread_id: str,
        limit: int = 20,
    ) -> "EventListResponse | None":
        ...
