"""Chat behavior services."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ...kernel.events import EventEnvelope, EventSummary, encode_event_cursor
from ...kernel.storage import session_scope
from ...transcript_service import sanitize_text
from ...transports.discord.threads import ThreadManager
from .conversation_service import get_or_create_general_conversation
from .models import (
    ChatConversationModel,
    ChatMessageModel,
    ChatParticipantModel,
    ChatParticipantStateModel,
    ChatThreadModel,
)
from .schemas import (
    ChatMessageSubmitResponse,
    ChatMessageSummary,
    ChatParticipantSummary,
    ChatThreadCreateResponse,
    ChatThreadDeltaResponse,
    ChatThreadSummary,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview_text(content: str, limit: int = 120) -> str:
    compact = " ".join(content.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


class ChatBehaviorService:
    def __init__(self, *, thread_manager: ThreadManager, subscription_broker=None) -> None:
        self.thread_manager = thread_manager
        self.subscription_broker = subscription_broker

    async def create_chat_thread(
        self,
        *,
        guild_id: str,
        parent_channel_id: str,
        title: str,
        topic: str | None,
        created_by: str,
        auto_archive_duration: int = 1440,
    ) -> ChatThreadCreateResponse:
        thread_id = await self.thread_manager.create_thread(
            guild_id=guild_id,
            parent_channel_id=parent_channel_id,
            title=title,
            starter_text=f"Codex dialogue space opened for `{title}`.",
            auto_archive_duration=auto_archive_duration,
        )
        with session_scope() as db:
            row = ChatThreadModel(
                guild_id=guild_id,
                parent_channel_id=parent_channel_id,
                discord_thread_id=thread_id,
                title=title,
                topic=topic,
                created_by=created_by,
            )
            db.add(row)
            db.flush()
            self._ensure_general_conversation(db=db, row=row)
            response = ChatThreadCreateResponse(
                id=row.id,
                discord_thread_id=row.discord_thread_id,
                title=row.title,
            )
        await self.thread_manager.post_message(
            thread_id,
            (
                "이 스레드는 서로 다른 PC의 Codex 인스턴스가 대화하는 room이다.\n"
                f"- 주제: `{topic or 'freeform'}`\n"
                "- 일반 메시지는 turn과 최근 발화를 기록한다.\n"
                "- `state` 또는 `/chat state`로 현재 대화 상태를 볼 수 있다."
            ),
        )
        return response

    def get_chat_thread(self, *, thread_id: str) -> ChatThreadSummary | None:
        with session_scope() as db:
            row = self._get_thread_row(db=db, thread_id=thread_id)
            if row is None:
                return None
            return self._thread_summary(row=row)

    def register_participant(
        self,
        *,
        thread_id: str,
        actor_name: str,
        actor_kind: str = "ai",
    ) -> ChatParticipantSummary | None:
        now = utcnow()
        with session_scope() as db:
            row = self._get_thread_row(db=db, thread_id=thread_id)
            if row is None:
                return None
            participant = self._ensure_participant(
                db=db,
                row=row,
                actor_name=actor_name,
                actor_kind=actor_kind,
            )
            state = self._ensure_participant_state(
                db=db,
                row=row,
                actor_name=actor_name,
            )
            state.last_seen_at = now
            return self._participant_summary(participant=participant, state=state)

    def heartbeat_participant(
        self,
        *,
        thread_id: str,
        actor_name: str,
    ) -> ChatParticipantSummary | None:
        now = utcnow()
        with session_scope() as db:
            row = self._get_thread_row(db=db, thread_id=thread_id)
            if row is None:
                return None
            participant = self._ensure_participant(
                db=db,
                row=row,
                actor_name=actor_name,
                actor_kind="ai",
            )
            state = self._ensure_participant_state(
                db=db,
                row=row,
                actor_name=actor_name,
            )
            state.last_seen_at = now
            return self._participant_summary(participant=participant, state=state)

    def get_thread_delta(
        self,
        *,
        thread_id: str,
        actor_name: str,
        after_message_id: str | None = None,
        limit: int = 20,
        mark_read: bool = False,
    ) -> ChatThreadDeltaResponse | None:
        now = utcnow()
        with session_scope() as db:
            row = self._get_thread_row(db=db, thread_id=thread_id)
            if row is None:
                return None

            participant = self._ensure_participant(
                db=db,
                row=row,
                actor_name=actor_name,
                actor_kind="ai",
            )
            state = self._ensure_participant_state(
                db=db,
                row=row,
                actor_name=actor_name,
            )
            state.last_seen_at = now

            all_messages = list(
                db.scalars(
                    select(ChatMessageModel)
                    .where(ChatMessageModel.thread_id == row.id)
                    .order_by(ChatMessageModel.created_at.asc()),
                ),
            )
            cursor_id = after_message_id or state.last_read_message_id
            messages = self._messages_after_cursor(all_messages=all_messages, cursor_id=cursor_id, limit=limit)

            if mark_read and messages:
                last_message = messages[-1]
                state.last_read_message_id = last_message.id
                state.last_read_message_at = last_message.created_at

            participants = list(
                db.scalars(
                    select(ChatParticipantModel)
                    .where(ChatParticipantModel.thread_id == row.id)
                    .order_by(ChatParticipantModel.actor_name.asc()),
                ),
            )
            state_by_actor = {
                item.actor_name: item
                for item in db.scalars(
                    select(ChatParticipantStateModel).where(ChatParticipantStateModel.thread_id == row.id),
                )
            }

            return ChatThreadDeltaResponse(
                thread=self._thread_summary(row=row),
                participant=self._participant_summary(participant=participant, state=state),
                participants=[
                    self._participant_summary(
                        participant=item,
                        state=state_by_actor.get(item.actor_name),
                    )
                    for item in participants
                ],
                messages=[self._message_summary(message=item) for item in messages],
            )

    def submit_participant_message(
        self,
        *,
        thread_id: str,
        actor_name: str,
        content: str,
        actor_kind: str = "ai",
    ) -> ChatMessageSubmitResponse | None:
        now = utcnow()
        envelope: EventEnvelope | None = None
        with session_scope() as db:
            row = self._get_thread_row(db=db, thread_id=thread_id)
            if row is None:
                return None

            participant = self._ensure_participant(
                db=db,
                row=row,
                actor_name=actor_name,
                actor_kind=actor_kind,
            )
            state = self._ensure_participant_state(
                db=db,
                row=row,
                actor_name=actor_name,
            )
            general = self._ensure_general_conversation(db=db, row=row)
            message = self._append_message(
                db=db,
                row=row,
                participant=participant,
                actor_name=actor_name,
                content=content,
                now=now,
                conversation=general,
            )
            state.last_seen_at = now
            state.last_read_message_id = message.id
            state.last_read_message_at = message.created_at
            envelope = EventEnvelope(
                cursor=encode_event_cursor(created_at=message.created_at, event_id=message.id),
                space_id=row.id,
                event=EventSummary(
                    id=message.id,
                    kind=message.event_kind,
                    actor_name=message.actor_name,
                    content=message.content,
                    created_at=message.created_at,
                ),
            )
            response = ChatMessageSubmitResponse(
                thread=self._thread_summary(row=row),
                participant=self._participant_summary(participant=participant, state=state),
                message=self._message_summary(message=message),
            )
        if envelope is not None and self.subscription_broker is not None:
            self.subscription_broker.publish(space_id=envelope.space_id, item=envelope)
        return response

    async def submit_participant_message_and_notify(
        self,
        *,
        thread_id: str,
        actor_name: str,
        content: str,
        actor_kind: str = "ai",
    ) -> ChatMessageSubmitResponse | None:
        response = self.submit_participant_message(
            thread_id=thread_id,
            actor_name=actor_name,
            actor_kind=actor_kind,
            content=content,
        )
        if response is None:
            return None
        await self.thread_manager.post_message(
            thread_id,
            self._format_discord_transport_message(
                actor_name=response.message.actor_name,
                content=response.message.content,
            ),
        )
        return response

    def record_message(self, *, thread_id: str, actor_name: str, content: str) -> ChatThreadSummary | None:
        response = self.submit_participant_message(
            thread_id=thread_id,
            actor_name=actor_name,
            actor_kind="participant",
            content=content,
        )
        if response is None:
            return None
        return response.thread

    @staticmethod
    def _get_thread_row(*, db, thread_id: str) -> ChatThreadModel | None:
        return db.scalar(select(ChatThreadModel).where(ChatThreadModel.discord_thread_id == thread_id))

    @staticmethod
    def _thread_summary(*, row: ChatThreadModel) -> ChatThreadSummary:
        return ChatThreadSummary(
            id=row.id,
            discord_thread_id=row.discord_thread_id,
            title=row.title,
            topic=row.topic,
            status=row.status,
            turn_count=row.turn_count,
            last_actor_name=row.last_actor_name,
            last_message_preview=row.last_message_preview,
            last_message_at=row.last_message_at,
            created_at=row.created_at,
        )

    @staticmethod
    def _message_summary(*, message: ChatMessageModel) -> ChatMessageSummary:
        return ChatMessageSummary(
            id=message.id,
            actor_name=message.actor_name,
            event_kind=message.event_kind,
            content=message.content,
            created_at=message.created_at,
        )

    @staticmethod
    def _participant_summary(
        *,
        participant: ChatParticipantModel,
        state: ChatParticipantStateModel | None,
    ) -> ChatParticipantSummary:
        return ChatParticipantSummary(
            actor_name=participant.actor_name,
            actor_kind=participant.kind,
            turn_count=participant.turn_count or 0,
            last_message_preview=participant.last_message_preview,
            last_message_at=participant.last_message_at,
            last_seen_at=state.last_seen_at if state is not None else None,
            last_read_message_id=state.last_read_message_id if state is not None else None,
            last_read_message_at=state.last_read_message_at if state is not None else None,
        )

    @staticmethod
    def _ensure_participant(
        *,
        db,
        row: ChatThreadModel,
        actor_name: str,
        actor_kind: str,
    ) -> ChatParticipantModel:
        participant = db.scalar(
            select(ChatParticipantModel)
            .where(ChatParticipantModel.thread_id == row.id)
            .where(ChatParticipantModel.actor_name == actor_name),
        )
        if participant is None:
            participant = ChatParticipantModel(
                thread_id=row.id,
                actor_name=actor_name,
                kind=actor_kind,
            )
            db.add(participant)
        elif actor_kind and participant.kind != actor_kind:
            participant.kind = actor_kind
        return participant

    @staticmethod
    def _ensure_participant_state(
        *,
        db,
        row: ChatThreadModel,
        actor_name: str,
    ) -> ChatParticipantStateModel:
        state = db.scalar(
            select(ChatParticipantStateModel)
            .where(ChatParticipantStateModel.thread_id == row.id)
            .where(ChatParticipantStateModel.actor_name == actor_name),
        )
        if state is None:
            state = ChatParticipantStateModel(
                thread_id=row.id,
                actor_name=actor_name,
            )
            db.add(state)
        return state

    @staticmethod
    def _ensure_general_conversation(
        *,
        db,
        row: ChatThreadModel,
    ) -> ChatConversationModel:
        # Delegate to the shared helper in conversation_service so chat
        # behavior and the conversation protocol service agree on the
        # exact bootstrap shape.
        return get_or_create_general_conversation(db, row)

    @staticmethod
    def _append_message(
        *,
        db,
        row: ChatThreadModel,
        participant: ChatParticipantModel,
        actor_name: str,
        content: str,
        now: datetime,
        conversation: ChatConversationModel,
    ) -> ChatMessageModel:
        clean_content = sanitize_text(content)
        preview = _preview_text(clean_content)

        row.turn_count += 1
        row.last_actor_name = actor_name
        row.last_message_preview = preview
        row.last_message_at = now

        message = ChatMessageModel(
            thread_id=row.id,
            conversation_id=conversation.id,
            actor_name=actor_name,
            content=clean_content,
        )
        db.add(message)
        db.flush()

        conversation.last_speech_at = now
        conversation.speech_count = (conversation.speech_count or 0) + 1

        participant.turn_count = (participant.turn_count or 0) + 1
        participant.last_message_preview = preview
        participant.last_message_at = now
        return message

    @staticmethod
    def _messages_after_cursor(
        *,
        all_messages: list[ChatMessageModel],
        cursor_id: str | None,
        limit: int,
    ) -> list[ChatMessageModel]:
        if not all_messages:
            return []
        if not cursor_id:
            return all_messages[-limit:]

        start_index = -1
        for index, message in enumerate(all_messages):
            if message.id == cursor_id:
                start_index = index
                break
        unread = all_messages[start_index + 1 :] if start_index >= 0 else all_messages
        return unread[:limit]

    @staticmethod
    def _format_discord_transport_message(*, actor_name: str, content: str) -> str:
        return f"**{actor_name}**: {content}"
