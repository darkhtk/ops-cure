from __future__ import annotations

from typing import Protocol


class ChatParticipantStateStore(Protocol):
    def get_cursor(self, *, actor_name: str, thread_id: str) -> str | None: ...

    def set_cursor(self, *, actor_name: str, thread_id: str, message_id: str) -> None: ...


class InMemoryChatParticipantStateStore:
    def __init__(self) -> None:
        self._cursors: dict[tuple[str, str], str] = {}

    def get_cursor(self, *, actor_name: str, thread_id: str) -> str | None:
        return self._cursors.get((actor_name, thread_id))

    def set_cursor(self, *, actor_name: str, thread_id: str, message_id: str) -> None:
        self._cursors[(actor_name, thread_id)] = message_id
