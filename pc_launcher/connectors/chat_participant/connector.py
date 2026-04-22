from __future__ import annotations

from dataclasses import dataclass

from .bridge import ChatParticipantBridge
from .runtime import ChatParticipantRuntime, ReplyContext
from .state_store import ChatParticipantStateStore


@dataclass(slots=True)
class ChatParticipantConfig:
    actor_name: str
    actor_kind: str = "ai"
    machine_label: str | None = None
    default_thread_id: str | None = None
    allow_unprompted: bool = False
    delta_limit: int = 20


@dataclass(slots=True)
class ChatSyncResult:
    status: str
    thread_id: str
    reason: str
    replied_message_id: str | None = None
    seen_message_id: str | None = None


class ChatParticipantConnector:
    def __init__(
        self,
        *,
        bridge: ChatParticipantBridge,
        runtime: ChatParticipantRuntime,
        state_store: ChatParticipantStateStore,
        config: ChatParticipantConfig,
    ) -> None:
        self.bridge = bridge
        self.runtime = runtime
        self.state_store = state_store
        self.config = config

    def sync_once(self, *, thread_id: str | None = None) -> ChatSyncResult:
        room_thread_id = thread_id or self.config.default_thread_id
        if not room_thread_id:
            raise ValueError("thread_id is required when no default_thread_id is configured.")

        space = self.bridge.get_space_by_thread(thread_id=room_thread_id)
        self.bridge.register_chat_participant(
            thread_id=room_thread_id,
            actor_name=self.config.actor_name,
            actor_kind=self.config.actor_kind,
        )
        actors = self.bridge.get_actors_for_space(space_id=space["id"])
        cursor = self.state_store.get_cursor(
            actor_name=self.config.actor_name,
            thread_id=room_thread_id,
        )
        delta = self.bridge.get_chat_delta(
            thread_id=room_thread_id,
            actor_name=self.config.actor_name,
            after_message_id=cursor,
            limit=self.config.delta_limit,
            mark_read=False,
        )
        self.bridge.heartbeat_chat_participant(
            thread_id=room_thread_id,
            actor_name=self.config.actor_name,
        )

        messages = list(delta.get("messages") or [])
        if not messages:
            return ChatSyncResult(
                status="idle",
                thread_id=room_thread_id,
                reason="no_unread_messages",
            )

        latest_seen_id = messages[-1]["id"]
        target_message = self._select_target_message(messages=messages)
        if target_message is None:
            self.state_store.set_cursor(
                actor_name=self.config.actor_name,
                thread_id=room_thread_id,
                message_id=latest_seen_id,
            )
            return ChatSyncResult(
                status="skipped",
                thread_id=room_thread_id,
                reason="self_only_messages",
                seen_message_id=latest_seen_id,
            )

        if not self._is_reply_allowed(
            target_message=target_message,
            participants=delta.get("participants") or actors.get("actors") or [],
        ):
            self.state_store.set_cursor(
                actor_name=self.config.actor_name,
                thread_id=room_thread_id,
                message_id=latest_seen_id,
            )
            return ChatSyncResult(
                status="skipped",
                thread_id=room_thread_id,
                reason="not_addressed_to_actor",
                seen_message_id=latest_seen_id,
            )

        context = ReplyContext(
            actor_name=self.config.actor_name,
            actor_kind=self.config.actor_kind,
            thread_id=room_thread_id,
            space_id=space["id"],
            room_title=space["title"],
            room_topic=space.get("metadata", {}).get("topic"),
            machine_label=self.config.machine_label,
            participants=list(delta.get("participants") or actors.get("actors") or []),
            recent_messages=messages,
        )
        reply = self.runtime.generate_reply(context)

        self.state_store.set_cursor(
            actor_name=self.config.actor_name,
            thread_id=room_thread_id,
            message_id=latest_seen_id,
        )
        if reply is None or not reply.content.strip():
            return ChatSyncResult(
                status="skipped",
                thread_id=room_thread_id,
                reason="runtime_returned_no_reply",
                seen_message_id=latest_seen_id,
            )

        submitted = self.bridge.submit_chat_message(
            thread_id=room_thread_id,
            actor_name=self.config.actor_name,
            actor_kind=self.config.actor_kind,
            content=reply.content,
        )
        replied_message_id = submitted["message"]["id"]
        self.state_store.set_cursor(
            actor_name=self.config.actor_name,
            thread_id=room_thread_id,
            message_id=replied_message_id,
        )
        return ChatSyncResult(
            status="replied",
            thread_id=room_thread_id,
            reason="reply_submitted",
            replied_message_id=replied_message_id,
            seen_message_id=latest_seen_id,
        )

    def _select_target_message(self, *, messages: list[dict[str, object]]) -> dict[str, object] | None:
        for message in reversed(messages):
            if str(message.get("actor_name") or "") != self.config.actor_name:
                return message
        return None

    def _is_reply_allowed(
        self,
        *,
        target_message: dict[str, object],
        participants: list[dict[str, object]],
    ) -> bool:
        content = str(target_message.get("content") or "")
        if self._is_explicitly_targeted(content=content):
            return True
        if not self.config.allow_unprompted:
            return False
        other_participants = [
            item
            for item in participants
            if str(item.get("actor_name") or item.get("name") or "") != self.config.actor_name
        ]
        return bool(other_participants)

    def _is_explicitly_targeted(self, *, content: str) -> bool:
        lowered = content.lower()
        actor = self.config.actor_name.lower()
        return (
            f"@{actor}" in lowered
            or lowered.startswith(f"{actor}:")
            or f"[{actor}]" in lowered
        )
