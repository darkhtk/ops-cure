"""Kernel bindings for behavior-provided space/actor/event providers."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ActorProvider, EventProvider, SpaceProvider


@dataclass(frozen=True, slots=True)
class KernelBehaviorBinding:
    behavior_id: str
    space_provider: SpaceProvider | None = None
    actor_provider: ActorProvider | None = None
    event_provider: EventProvider | None = None
