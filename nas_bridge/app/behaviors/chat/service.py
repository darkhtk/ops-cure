"""Chat behavior services."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ...kernel.storage import session_scope
from ...transcript_service import sanitize_text
from ...transports.discord.threads import ThreadManager
from .models import ChatMessageModel, ChatParticipantModel, ChatThreadModel
from .schemas import ChatThreadCreateResponse, ChatThreadSummary


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview_text(content: str, limit: int = 120) -> str:
    compact = " ".join(content.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


class ChatBehaviorService:
    def __init__(self, *, thread_manager: ThreadManager) -> None:
        self.thread_manager = thread_manager

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
                "- 일반 메시지는 turn과 최근 발화자를 기록한다.\n"
                "- `state` 또는 `/chat state`로 현재 대화 상태를 볼 수 있다."
            ),
        )
        return response

    def get_chat_thread(self, *, thread_id: str) -> ChatThreadSummary | None:
        with session_scope() as db:
            row = db.scalar(select(ChatThreadModel).where(ChatThreadModel.discord_thread_id == thread_id))
            if row is None:
                return None
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

    def record_message(self, *, thread_id: str, actor_name: str, content: str) -> ChatThreadSummary | None:
        with session_scope() as db:
            row = db.scalar(select(ChatThreadModel).where(ChatThreadModel.discord_thread_id == thread_id))
            if row is None:
                return None

            clean_content = sanitize_text(content)
            preview = _preview_text(clean_content)
            now = utcnow()

            row.turn_count += 1
            row.last_actor_name = actor_name
            row.last_message_preview = preview
            row.last_message_at = now

            db.add(
                ChatMessageModel(
                    thread_id=row.id,
                    actor_name=actor_name,
                    content=clean_content,
                ),
            )

            participant = db.scalar(
                select(ChatParticipantModel)
                .where(ChatParticipantModel.thread_id == row.id)
                .where(ChatParticipantModel.actor_name == actor_name),
            )
            if participant is None:
                participant = ChatParticipantModel(
                    thread_id=row.id,
                    actor_name=actor_name,
                )
                db.add(participant)
            participant.turn_count = (participant.turn_count or 0) + 1
            participant.last_message_preview = preview
            participant.last_message_at = now

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
