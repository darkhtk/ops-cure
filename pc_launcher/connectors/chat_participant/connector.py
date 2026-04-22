from __future__ import annotations

import threading
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
    allow_unprompted: bool = True
    delta_limit: int = 20
    progress_notice_delay_seconds: float = 3.0


@dataclass(slots=True)
class ChatSyncResult:
    status: str
    thread_id: str
    reason: str
    replied_message_id: str | None = None
    seen_message_id: str | None = None
    progress_message_id: str | None = None


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
        reply, progress_message_id = self._generate_reply_with_progress(
            context=context,
            thread_id=room_thread_id,
            target_message=target_message,
        )

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
                progress_message_id=progress_message_id,
            )

        submitted = self.bridge.submit_chat_message(
            thread_id=room_thread_id,
            actor_name=self.config.actor_name,
            actor_kind=self.config.actor_kind,
            content=reply.content,
        )
        replied_message_id = submitted["message"]["id"]
        return ChatSyncResult(
            status="replied",
            thread_id=room_thread_id,
            reason="reply_submitted",
            replied_message_id=replied_message_id,
            seen_message_id=latest_seen_id,
            progress_message_id=progress_message_id,
        )

    def _generate_reply_with_progress(
        self,
        *,
        context: ReplyContext,
        thread_id: str,
        target_message: dict[str, object],
    ):
        reply_holder: dict[str, object] = {}
        error_holder: dict[str, BaseException] = {}
        progress_message_id: str | None = None

        def worker() -> None:
            try:
                reply_holder["reply"] = self.runtime.generate_reply(context)
            except BaseException as exc:  # pragma: no cover - exercised through caller path
                error_holder["error"] = exc

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=max(0.0, float(self.config.progress_notice_delay_seconds)))

        if worker_thread.is_alive():
            progress_message_id = self._submit_progress_notice(
                thread_id=thread_id,
                target_message=target_message,
            )

        worker_thread.join()

        if "error" in error_holder:
            if progress_message_id is not None:
                self._submit_failure_notice(
                    thread_id=thread_id,
                    target_message=target_message,
                    error=error_holder["error"],
                )
            raise error_holder["error"]

        return reply_holder.get("reply"), progress_message_id

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

    def _submit_progress_notice(self, *, thread_id: str, target_message: dict[str, object]) -> str:
        submitted = self.bridge.submit_chat_message(
            thread_id=thread_id,
            actor_name=self.config.actor_name,
            actor_kind=self.config.actor_kind,
            content=self._build_progress_notice(target_message=target_message),
        )
        return str(submitted["message"]["id"])

    def _submit_failure_notice(
        self,
        *,
        thread_id: str,
        target_message: dict[str, object],
        error: BaseException,
    ) -> None:
        self.bridge.submit_chat_message(
            thread_id=thread_id,
            actor_name=self.config.actor_name,
            actor_kind=self.config.actor_kind,
            content=self._build_failure_notice(target_message=target_message, error=error),
        )

    def _build_progress_notice(self, *, target_message: dict[str, object]) -> str:
        content = str(target_message.get("content") or "")
        if self._looks_like_korean(content):
            return "확인했다. 지금 바로 확인하고 진행 중이다. 끝나면 여기 보고하겠다."
        return "I saw the request and I'm working on it now. I'll report back here when I have a concrete result."

    def _build_failure_notice(
        self,
        *,
        target_message: dict[str, object],
        error: BaseException,
    ) -> str:
        detail = " ".join(str(error).split()) or error.__class__.__name__
        detail = detail[:200]
        content = str(target_message.get("content") or "")
        if self._looks_like_korean(content):
            return f"작업을 진행하다가 오류가 났다: {detail}"
        return f"I hit an error while working on it: {detail}"

    def _looks_like_korean(self, content: str) -> bool:
        return any("\uac00" <= char <= "\ud7a3" for char in content)
