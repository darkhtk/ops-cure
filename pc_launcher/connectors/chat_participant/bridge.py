from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ...bridge_client import BridgeClient


class ChatParticipantBridge(Protocol):
    def get_space_by_thread(self, *, thread_id: str) -> dict[str, Any]: ...

    def get_actors_for_space(self, *, space_id: str) -> dict[str, Any]: ...

    def register_chat_participant(
        self,
        *,
        thread_id: str,
        actor_name: str,
        actor_kind: str = "ai",
    ) -> dict[str, Any]: ...

    def heartbeat_chat_participant(self, *, thread_id: str, actor_name: str) -> dict[str, Any]: ...

    def get_chat_delta(
        self,
        *,
        thread_id: str,
        actor_name: str,
        after_message_id: str | None = None,
        limit: int = 20,
        mark_read: bool = False,
    ) -> dict[str, Any]: ...

    def submit_chat_message(
        self,
        *,
        thread_id: str,
        actor_name: str,
        content: str,
        actor_kind: str = "ai",
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class BridgeChatParticipantClient:
    bridge_client: BridgeClient

    def get_space_by_thread(self, *, thread_id: str) -> dict[str, Any]:
        return self.bridge_client.get_space_by_thread(thread_id=thread_id)

    def get_actors_for_space(self, *, space_id: str) -> dict[str, Any]:
        return self.bridge_client.get_actors_for_space(space_id=space_id)

    def register_chat_participant(
        self,
        *,
        thread_id: str,
        actor_name: str,
        actor_kind: str = "ai",
    ) -> dict[str, Any]:
        return self.bridge_client.register_chat_participant(
            thread_id=thread_id,
            actor_name=actor_name,
            actor_kind=actor_kind,
        )

    def heartbeat_chat_participant(self, *, thread_id: str, actor_name: str) -> dict[str, Any]:
        return self.bridge_client.heartbeat_chat_participant(
            thread_id=thread_id,
            actor_name=actor_name,
        )

    def get_chat_delta(
        self,
        *,
        thread_id: str,
        actor_name: str,
        after_message_id: str | None = None,
        limit: int = 20,
        mark_read: bool = False,
    ) -> dict[str, Any]:
        return self.bridge_client.get_chat_delta(
            thread_id=thread_id,
            actor_name=actor_name,
            after_message_id=after_message_id,
            limit=limit,
            mark_read=mark_read,
        )

    def submit_chat_message(
        self,
        *,
        thread_id: str,
        actor_name: str,
        content: str,
        actor_kind: str = "ai",
    ) -> dict[str, Any]:
        return self.bridge_client.submit_chat_message(
            thread_id=thread_id,
            actor_name=actor_name,
            content=content,
            actor_kind=actor_kind,
        )
