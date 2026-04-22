from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ReplyContext:
    actor_name: str
    actor_kind: str
    thread_id: str
    space_id: str
    room_title: str
    room_topic: str | None
    machine_label: str | None
    participants: list[dict[str, Any]] = field(default_factory=list)
    recent_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ReplyResult:
    content: str


class ChatParticipantRuntime(Protocol):
    def generate_reply(self, context: ReplyContext) -> ReplyResult | None: ...
