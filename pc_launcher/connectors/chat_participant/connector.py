from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

from .bridge import ChatParticipantBridge
from .runtime import ChatParticipantRuntime, ReplyContext
from .state_store import ChatParticipantStateStore


PROGRESS_NOTICE_KO = (
    "\ud655\uc778\ud588\ub2e4. \uc9c0\uae08 \ubc14\ub85c \ud655\uc778\ud558\uace0 "
    "\uc9c4\ud589 \uc911\uc774\ub2e4. \ub05d\ub098\uba74 \uc5ec\uae30 \ubcf4\uace0\ud558\uaca0\ub2e4."
)
PROGRESS_NOTICE_EN = "I saw the request and I'm working on it now. I'll report back here when I have a concrete result."
FAILURE_NOTICE_KO_PREFIX = "\uc791\uc5c5\uc744 \uc9c4\ud589\ud558\ub2e4\uac00 \uc624\ub958\uac00 \ub0ac\ub2e4:"
FAILURE_NOTICE_EN_PREFIX = "I hit an error while working on it:"


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

        reply_gate_reason = self._reply_gate_reason(
            target_message=target_message,
            participants=delta.get("participants") or actors.get("actors") or [],
            recent_messages=messages,
        )
        if reply_gate_reason is not None:
            self.state_store.set_cursor(
                actor_name=self.config.actor_name,
                thread_id=room_thread_id,
                message_id=latest_seen_id,
            )
            return ChatSyncResult(
                status="skipped",
                thread_id=room_thread_id,
                reason=reply_gate_reason,
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
        emit_progress_notice = self._should_emit_progress_notice(
            target_message=target_message,
            participants=delta.get("participants") or actors.get("actors") or [],
        )
        reply, progress_message_id = self._generate_reply_with_progress(
            context=context,
            thread_id=room_thread_id,
            target_message=target_message,
            emit_progress_notice=emit_progress_notice,
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
        emit_progress_notice: bool,
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

        if emit_progress_notice and worker_thread.is_alive():
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

    def _reply_gate_reason(
        self,
        *,
        target_message: dict[str, object],
        participants: list[dict[str, object]],
        recent_messages: list[dict[str, object]],
    ) -> str | None:
        content = str(target_message.get("content") or "")
        if self._is_control_message(content=content):
            return "control_message"
        if self._is_explicitly_targeted(content=content):
            return None
        actor_kind = self._actor_kind_for_message(
            target_message=target_message,
            participants=participants,
        )
        if actor_kind == "ai":
            if self._is_meaningless_ai_echo(
                target_message=target_message,
                recent_messages=recent_messages,
            ):
                return "ai_echo_message"
            return None
        if not self.config.allow_unprompted:
            return "not_addressed_to_actor"
        if not self._claims_unprompted_turn(target_message=target_message, participants=participants):
            return "turn_claimed_by_other_participant"
        return None

    def _should_emit_progress_notice(
        self,
        *,
        target_message: dict[str, object],
        participants: list[dict[str, object]],
    ) -> bool:
        actor_kind = self._actor_kind_for_message(
            target_message=target_message,
            participants=participants,
        )
        return actor_kind != "ai"

    def _is_explicitly_targeted(self, *, content: str) -> bool:
        lowered = content.lower()
        actor = self.config.actor_name.lower()
        return (
            f"@{actor}" in lowered
            or lowered.startswith(f"{actor}:")
            or f"[{actor}]" in lowered
        )

    def _actor_kind_for_message(
        self,
        *,
        target_message: dict[str, object],
        participants: list[dict[str, object]],
    ) -> str | None:
        actor_name = str(target_message.get("actor_name") or "")
        for participant in participants:
            name = str(participant.get("actor_name") or participant.get("name") or "")
            if name == actor_name:
                return str(participant.get("actor_kind") or participant.get("kind") or "")
        return None

    def _claims_unprompted_turn(
        self,
        *,
        target_message: dict[str, object],
        participants: list[dict[str, object]],
    ) -> bool:
        ai_names = sorted(
            {
                str(item.get("actor_name") or item.get("name") or "")
                for item in participants
                if str(item.get("actor_kind") or item.get("kind") or "") == "ai"
                and str(item.get("actor_name") or item.get("name") or "")
            },
        )
        if not ai_names:
            return True
        if self.config.actor_name not in ai_names:
            ai_names.append(self.config.actor_name)
            ai_names.sort()
        if len(ai_names) == 1:
            return True
        claim_seed = str(target_message.get("id") or "") or (
            f"{target_message.get('actor_name') or ''}:{target_message.get('content') or ''}"
        )
        digest = hashlib.sha256(claim_seed.encode("utf-8")).digest()
        claimed_actor = ai_names[int.from_bytes(digest[:4], "big") % len(ai_names)]
        return claimed_actor == self.config.actor_name

    def _is_control_message(self, *, content: str) -> bool:
        lowered = self._normalize_text(content)
        if lowered in {"\uba48\ucdb0", "stop"}:
            return True
        control_markers = (
            "\ub2e4 \ub300\ud654 \uba48\ucdb0",
            "\ub300\ub2f5\ub3c4 \ud558\uc9c0\ub9c8",
            "\ub300\ub2f5\ud558\uc9c0 \ub9c8",
            "\uc751\ub2f5\ud558\uc9c0 \ub9c8",
            "\ub9d0\ud558\uc9c0 \ub9c8",
            "\uc870\uc6a9\ud788",
            "stop talking",
            "stop replying",
            "don't reply",
            "do not reply",
            "be quiet",
            "silence this room",
        )
        return any(marker in lowered for marker in control_markers)

    def _is_meaningless_ai_echo(
        self,
        *,
        target_message: dict[str, object],
        recent_messages: list[dict[str, object]],
    ) -> bool:
        content = self._normalize_text(str(target_message.get("content") or ""))
        if not content:
            return True
        if self._is_progress_notice_text(content) or self._is_failure_notice_text(content):
            return True
        if self._is_planning_churn_text(content):
            return True
        repeated_actors = {
            str(message.get("actor_name") or "")
            for message in recent_messages
            if self._normalize_text(str(message.get("content") or "")) == content
        }
        return len(repeated_actors) >= 2

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
            return PROGRESS_NOTICE_KO
        return PROGRESS_NOTICE_EN

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
            return f"{FAILURE_NOTICE_KO_PREFIX} {detail}"
        return f"{FAILURE_NOTICE_EN_PREFIX} {detail}"

    def _looks_like_korean(self, content: str) -> bool:
        return any("\uac00" <= char <= "\ud7a3" for char in content)

    def _is_progress_notice_text(self, content: str) -> bool:
        normalized = self._normalize_text(content)
        return normalized in {
            self._normalize_text(PROGRESS_NOTICE_KO),
            self._normalize_text(PROGRESS_NOTICE_EN),
        }

    def _is_failure_notice_text(self, content: str) -> bool:
        normalized = self._normalize_text(content)
        return normalized.startswith(self._normalize_text(FAILURE_NOTICE_KO_PREFIX)) or normalized.startswith(
            self._normalize_text(FAILURE_NOTICE_EN_PREFIX),
        )

    def _is_planning_churn_text(self, content: str) -> bool:
        normalized = self._normalize_text(content)
        if not normalized:
            return False

        acknowledgement_markers = (
            "좋다",
            "알겠다",
            "확인했다",
            "sounds good",
            "got it",
            "understood",
        )
        planning_markers = (
            "내 쪽은",
            "나는 ",
            "우선순위는",
            "집중한다",
            "계속 본다",
            "맡는다",
            "순서로 간다",
            "커밋",
            "해시",
            "테스트 결과",
            "회귀",
            "확인 포인트",
            "그 기준으로",
            "다시 올리겠다",
            "공유하겠다",
            "넘기겠다",
            "i will",
            "my side",
            "priority is",
            "commit",
            "hash",
            "test results",
            "regression",
            "i'll verify",
            "i will verify",
            "report back",
            "share only",
        )

        has_acknowledgement = any(marker in normalized for marker in acknowledgement_markers)
        planning_hits = sum(1 for marker in planning_markers if marker in normalized)
        has_numbered_list = any(token in normalized for token in ("1.", "2.", "3.", "4.", "5.", "6.", "7."))

        return planning_hits >= 2 or (has_acknowledgement and (planning_hits >= 1 or has_numbered_list))

    def _normalize_text(self, content: str) -> str:
        return " ".join(content.lower().split())
