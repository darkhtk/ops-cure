"""Kernel provider contracts for behavior plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .actors import ActorListResponse
    from .events import EventDeltaResponse, EventEnvelope
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

    def get_events_for_space(
        self,
        *,
        space_id: str,
        after_cursor: str | None = None,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> "EventDeltaResponse | None":
        ...

    def get_events_for_thread(
        self,
        *,
        thread_id: str,
        after_cursor: str | None = None,
        limit: int = 20,
        kinds: list[str] | None = None,
    ) -> "EventDeltaResponse | None":
        ...


class SubscriptionBroker(Protocol):
    def publish(self, *, space_id: str, item: "EventEnvelope") -> None:
        ...

    def subscribe(
        self,
        *,
        space_id: str,
        after_cursor: str | None = None,
        kinds: list[str] | None = None,
        subscriber_id: str | None = None,
    ):
        ...
