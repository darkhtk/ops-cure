"""Chat kernel providers for generic Space/Actor/Event views."""

from __future__ import annotations

from sqlalchemy import select

from ...kernel.actors import ActorListResponse, ActorSummary
from ...kernel.bindings import KernelBehaviorBinding
from ...kernel.events import EventListResponse, EventSummary
from ...kernel.spaces import SpaceSummary
from ...kernel.storage import session_scope
from .models import ChatMessageModel, ChatParticipantModel, ChatThreadModel


class ChatKernelProvider:
    behavior_id = "chat"

    def get_space(self, *, space_id: str) -> SpaceSummary | None:
        with session_scope() as db:
            chat_row = db.scalar(select(ChatThreadModel).where(ChatThreadModel.id == space_id))
            if chat_row is None:
                return None
            return self._space_summary(chat_row)

    def get_space_by_thread(self, *, thread_id: str) -> SpaceSummary | None:
        with session_scope() as db:
            chat_row = db.scalar(
                select(ChatThreadModel).where(ChatThreadModel.discord_thread_id == thread_id),
            )
            if chat_row is None:
                return None
            return self._space_summary(chat_row)

    def get_actors_for_space(self, *, space_id: str) -> ActorListResponse | None:
        with session_scope() as db:
            chat_row = db.scalar(select(ChatThreadModel).where(ChatThreadModel.id == space_id))
            if chat_row is None:
                return None
            participants = list(
                db.scalars(
                    select(ChatParticipantModel)
                    .where(ChatParticipantModel.thread_id == chat_row.id)
                    .order_by(
                        ChatParticipantModel.last_message_at.desc().nullslast(),
                        ChatParticipantModel.actor_name,
                    ),
                ),
            )
            return self._actor_response(chat_row.id, participants)

    def get_actors_for_thread(self, *, thread_id: str) -> ActorListResponse | None:
        with session_scope() as db:
            chat_row = db.scalar(
                select(ChatThreadModel).where(ChatThreadModel.discord_thread_id == thread_id),
            )
            if chat_row is None:
                return None
            participants = list(
                db.scalars(
                    select(ChatParticipantModel)
                    .where(ChatParticipantModel.thread_id == chat_row.id)
                    .order_by(
                        ChatParticipantModel.last_message_at.desc().nullslast(),
                        ChatParticipantModel.actor_name,
                    ),
                ),
            )
            return self._actor_response(chat_row.id, participants)

    def get_events_for_space(self, *, space_id: str, limit: int = 20) -> EventListResponse | None:
        with session_scope() as db:
            chat_row = db.scalar(select(ChatThreadModel).where(ChatThreadModel.id == space_id))
            if chat_row is None:
                return None
            events = list(
                db.scalars(
                    select(ChatMessageModel)
                    .where(ChatMessageModel.thread_id == chat_row.id)
                    .order_by(ChatMessageModel.created_at.desc())
                    .limit(limit),
                ),
            )
            return self._event_response(chat_row.id, events)

    def get_events_for_thread(self, *, thread_id: str, limit: int = 20) -> EventListResponse | None:
        with session_scope() as db:
            chat_row = db.scalar(
                select(ChatThreadModel).where(ChatThreadModel.discord_thread_id == thread_id),
            )
            if chat_row is None:
                return None
            events = list(
                db.scalars(
                    select(ChatMessageModel)
                    .where(ChatMessageModel.thread_id == chat_row.id)
                    .order_by(ChatMessageModel.created_at.desc())
                    .limit(limit),
                ),
            )
            return self._event_response(chat_row.id, events)

    def _space_summary(self, chat_row: ChatThreadModel) -> SpaceSummary:
        return SpaceSummary(
            id=chat_row.id,
            domain_type="chat",
            transport_kind="discord_thread",
            transport_address=chat_row.discord_thread_id,
            title=chat_row.title,
            status=chat_row.status,
            created_at=chat_row.created_at,
            updated_at=chat_row.updated_at,
            actors=[],
            metadata={
                "topic": chat_row.topic,
                "turn_count": chat_row.turn_count,
                "last_actor_name": chat_row.last_actor_name,
                "last_message_preview": chat_row.last_message_preview,
            },
        )

    def _actor_response(
        self,
        space_id: str,
        participants: list[ChatParticipantModel],
    ) -> ActorListResponse:
        return ActorListResponse(
            space_id=space_id,
            domain_type="chat",
            actors=[
                ActorSummary(
                    name=participant.actor_name,
                    kind=participant.kind,
                    status="active",
                    detail=participant.last_message_preview,
                    turns=participant.turn_count,
                    last_active_at=participant.last_message_at,
                )
                for participant in participants
            ],
        )

    def _event_response(
        self,
        space_id: str,
        events: list[ChatMessageModel],
    ) -> EventListResponse:
        return EventListResponse(
            space_id=space_id,
            domain_type="chat",
            events=[
                EventSummary(
                    id=event.id,
                    kind=event.event_kind,
                    actor_name=event.actor_name,
                    content=event.content,
                    created_at=event.created_at,
                )
                for event in events
            ],
        )


def build_chat_kernel_binding() -> KernelBehaviorBinding:
    provider = ChatKernelProvider()
    return KernelBehaviorBinding(
        behavior_id="chat",
        space_provider=provider,
        actor_provider=provider,
        event_provider=provider,
    )
