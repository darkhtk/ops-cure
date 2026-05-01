"""Chat conversation protocol service.

Why this exists:
    AI 협업룸 needs explicit *open* and *close* of collaboration units,
    not just a free-form message stream. Conversations carry typed
    intent (inquiry / proposal / task) and must reach a resolution.
    Casual chat lives in the always-open ``general`` conversation per
    room so every speech act has a home.

Design notes:
    * Each lifecycle transition (opened / closed / speech / address) is
      persisted as a ``ChatMessageModel`` row with a discriminating
      ``event_kind``. The existing chat kernel events stream already
      sources from this table, so SSE resume/replay continues to work
      unchanged for new event kinds.
    * ``general`` conversations are forced ``is_general=True`` and
      cannot be closed (raised as ``ValueError``); they are created on
      first need by ``ensure_general()`` and backfilled lazily.
    * The service is intentionally agnostic of Discord transport. The
      higher ``ChatBehaviorService`` and Discord bindings are responsible
      for human-readable rendering.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from ...kernel.events import EventEnvelope, EventSummary, encode_event_cursor
from ...kernel.storage import session_scope
from ...transcript_service import sanitize_text
from .conversation_schemas import (
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationOpenRequest,
    ConversationSummary,
    SpeechActSubmitRequest,
    SpeechActSummary,
)
from .models import (
    CONVERSATION_KIND_GENERAL,
    CONVERSATION_STATE_CLOSED,
    CONVERSATION_STATE_OPEN,
    ChatConversationModel,
    ChatMessageModel,
    ChatThreadModel,
)


GENERAL_TITLE = "General"
GENERAL_INTENT = "Casual chat and unstructured updates."

EVENT_CONVERSATION_OPENED = "chat.conversation.opened"
EVENT_CONVERSATION_CLOSED = "chat.conversation.closed"
EVENT_CONVERSATION_ADDRESSED = "chat.conversation.addressed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _speech_event_kind(kind: str) -> str:
    return f"chat.speech.{kind}"


class ChatConversationNotFoundError(LookupError):
    """Raised when a conversation cannot be located."""


class ChatThreadNotFoundError(LookupError):
    """Raised when the parent chat thread cannot be located."""


class ChatConversationStateError(ValueError):
    """Raised when a transition is not legal for the current conversation state."""


class ChatConversationService:
    def __init__(self, *, subscription_broker: Any | None = None) -> None:
        self._broker = subscription_broker

    # -------- thread / general bootstrap ------------------------------------

    def ensure_general(self, *, discord_thread_id: str) -> ConversationSummary:
        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)
            row = self._get_or_create_general(db, thread_row)
            return self._summary(row)

    def ensure_general_for_thread_id(self, *, thread_id: str) -> ConversationSummary:
        with session_scope() as db:
            thread_row = db.get(ChatThreadModel, thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(thread_id)
            row = self._get_or_create_general(db, thread_row)
            return self._summary(row)

    def backfill_general_conversations(self) -> int:
        """Ensure every room has a general conversation and orphan messages
        get attached to it. Idempotent. Returns the number of orphan
        messages migrated. Designed to run once at startup."""
        migrated = 0
        with session_scope() as db:
            for thread in db.scalars(select(ChatThreadModel)):
                general = self._get_or_create_general(db, thread)
                result = db.execute(
                    update(ChatMessageModel)
                    .where(ChatMessageModel.thread_id == thread.id)
                    .where(ChatMessageModel.conversation_id.is_(None))
                    .values(conversation_id=general.id),
                )
                migrated += result.rowcount or 0
                # Roll the legacy free-form "message" kind forward to
                # the typed "claim" speech kind. New writes always use
                # typed kinds so we only run this on legacy rows.
                db.execute(
                    update(ChatMessageModel)
                    .where(ChatMessageModel.thread_id == thread.id)
                    .where(ChatMessageModel.event_kind == "message")
                    .values(event_kind="claim"),
                )
        return migrated

    # -------- open / close ---------------------------------------------------

    def open_conversation(
        self,
        *,
        discord_thread_id: str,
        request: ConversationOpenRequest,
    ) -> ConversationSummary:
        envelope: EventEnvelope | None = None
        summary: ConversationSummary
        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)
            self._get_or_create_general(db, thread_row)

            row = ChatConversationModel(
                thread_id=thread_row.id,
                kind=request.kind,
                title=request.title,
                intent=request.intent,
                opener_actor=request.opener_actor,
                owner_actor=request.owner_actor or request.opener_actor,
                expected_speaker=request.addressed_to,
                parent_conversation_id=request.parent_conversation_id,
            )
            db.add(row)
            db.flush()

            payload = self._summary_payload(row)
            event_message = ChatMessageModel(
                thread_id=thread_row.id,
                conversation_id=row.id,
                actor_name=row.opener_actor,
                event_kind=EVENT_CONVERSATION_OPENED,
                addressed_to=row.expected_speaker,
                content=json.dumps(payload, ensure_ascii=False),
            )
            db.add(event_message)
            db.flush()
            envelope = self._envelope_for(thread_row.id, event_message)
            summary = self._summary(row)

        self._publish(envelope)
        return summary

    def close_conversation(
        self,
        *,
        conversation_id: str,
        closed_by: str,
        resolution: str,
        summary: str | None = None,
    ) -> ConversationSummary:
        envelope: EventEnvelope | None = None
        result: ConversationSummary
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)
            if row.is_general:
                raise ChatConversationStateError(
                    "general conversation cannot be closed",
                )
            if row.state == CONVERSATION_STATE_CLOSED:
                raise ChatConversationStateError(
                    f"conversation already closed (resolution={row.resolution})",
                )

            now = _utcnow()
            row.state = CONVERSATION_STATE_CLOSED
            row.resolution = resolution
            row.resolution_summary = summary
            row.closed_by = closed_by
            row.closed_at = now
            row.updated_at = now

            payload = self._summary_payload(row)
            event_message = ChatMessageModel(
                thread_id=row.thread_id,
                conversation_id=row.id,
                actor_name=closed_by,
                event_kind=EVENT_CONVERSATION_CLOSED,
                content=json.dumps(payload, ensure_ascii=False),
            )
            db.add(event_message)
            db.flush()
            envelope = self._envelope_for(row.thread_id, event_message)
            result = self._summary(row)

        self._publish(envelope)
        return result

    # -------- speech ---------------------------------------------------------

    def submit_speech(
        self,
        *,
        conversation_id: str,
        request: SpeechActSubmitRequest,
    ) -> SpeechActSummary:
        envelope: EventEnvelope | None = None
        summary: SpeechActSummary
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)
            if row.state == CONVERSATION_STATE_CLOSED:
                raise ChatConversationStateError(
                    "conversation is closed; reopen or start a new one",
                )

            clean = sanitize_text(request.content)
            now = _utcnow()
            message = ChatMessageModel(
                thread_id=row.thread_id,
                conversation_id=row.id,
                actor_name=request.actor_name,
                event_kind=_speech_event_kind(request.kind),
                addressed_to=request.addressed_to,
                content=clean,
            )
            db.add(message)
            db.flush()

            row.last_speech_at = now
            row.speech_count = (row.speech_count or 0) + 1
            if request.addressed_to:
                row.expected_speaker = request.addressed_to
            elif request.actor_name == row.expected_speaker:
                # the expected speaker just spoke — clear the slot
                row.expected_speaker = None
            row.updated_at = now

            envelope = self._envelope_for(row.thread_id, message)
            summary = self._speech_summary(message)

        self._publish(envelope)
        return summary

    # -------- listing / detail ----------------------------------------------

    def list_conversations(
        self,
        *,
        discord_thread_id: str,
        state: str | None = None,
        kind: str | None = None,
        include_general: bool = True,
        limit: int = 50,
    ) -> ConversationListResponse:
        with session_scope() as db:
            thread_row = self._get_thread_row_by_discord(db, discord_thread_id)
            if thread_row is None:
                raise ChatThreadNotFoundError(discord_thread_id)

            stmt = select(ChatConversationModel).where(
                ChatConversationModel.thread_id == thread_row.id,
            )
            if state is not None:
                stmt = stmt.where(ChatConversationModel.state == state)
            if kind is not None:
                stmt = stmt.where(ChatConversationModel.kind == kind)
            if not include_general:
                stmt = stmt.where(ChatConversationModel.is_general.is_(False))
            stmt = stmt.order_by(
                ChatConversationModel.is_general.desc(),
                ChatConversationModel.state.asc(),
                ChatConversationModel.updated_at.desc(),
            ).limit(limit)

            rows = list(db.scalars(stmt))
            return ConversationListResponse(
                thread_id=thread_row.id,
                conversations=[self._summary(row) for row in rows],
            )

    def get_conversation(
        self,
        *,
        conversation_id: str,
        recent: int = 30,
    ) -> ConversationDetailResponse:
        with session_scope() as db:
            row = db.get(ChatConversationModel, conversation_id)
            if row is None:
                raise ChatConversationNotFoundError(conversation_id)

            messages = list(
                db.scalars(
                    select(ChatMessageModel)
                    .where(ChatMessageModel.conversation_id == row.id)
                    .order_by(ChatMessageModel.created_at.desc())
                    .limit(recent),
                ),
            )
            messages.reverse()
            return ConversationDetailResponse(
                conversation=self._summary(row),
                recent_speech=[
                    self._speech_summary(message) for message in messages
                ],
            )

    # -------- internals -----------------------------------------------------

    @staticmethod
    def _get_thread_row_by_discord(db, discord_thread_id: str) -> ChatThreadModel | None:
        return db.scalar(
            select(ChatThreadModel).where(
                ChatThreadModel.discord_thread_id == discord_thread_id,
            ),
        )

    @staticmethod
    def _get_or_create_general(
        db,
        thread_row: ChatThreadModel,
    ) -> ChatConversationModel:
        general = db.scalar(
            select(ChatConversationModel)
            .where(ChatConversationModel.thread_id == thread_row.id)
            .where(ChatConversationModel.is_general.is_(True))
            .limit(1),
        )
        if general is not None:
            return general
        general = ChatConversationModel(
            thread_id=thread_row.id,
            kind=CONVERSATION_KIND_GENERAL,
            title=GENERAL_TITLE,
            intent=GENERAL_INTENT,
            state=CONVERSATION_STATE_OPEN,
            opener_actor="system",
            owner_actor=None,
            is_general=True,
        )
        db.add(general)
        db.flush()
        return general

    @staticmethod
    def _summary(row: ChatConversationModel) -> ConversationSummary:
        return ConversationSummary(
            id=row.id,
            thread_id=row.thread_id,
            kind=row.kind,
            title=row.title,
            intent=row.intent,
            state=row.state,
            opener_actor=row.opener_actor,
            owner_actor=row.owner_actor,
            expected_speaker=row.expected_speaker,
            parent_conversation_id=row.parent_conversation_id,
            bound_task_id=row.bound_task_id,
            resolution=row.resolution,
            resolution_summary=row.resolution_summary,
            closed_by=row.closed_by,
            is_general=bool(row.is_general),
            last_speech_at=row.last_speech_at,
            speech_count=row.speech_count or 0,
            created_at=row.created_at,
            closed_at=row.closed_at,
        )

    @classmethod
    def _summary_payload(cls, row: ChatConversationModel) -> dict[str, Any]:
        summary = cls._summary(row)
        return summary.model_dump(mode="json")

    @staticmethod
    def _speech_summary(message: ChatMessageModel) -> SpeechActSummary:
        kind = message.event_kind
        if kind.startswith("chat.speech."):
            kind = kind[len("chat.speech.") :]
        return SpeechActSummary(
            id=message.id,
            conversation_id=message.conversation_id or "",
            actor_name=message.actor_name,
            kind=kind,
            content=message.content,
            addressed_to=message.addressed_to,
            created_at=message.created_at,
        )

    @staticmethod
    def _envelope_for(thread_id: str, message: ChatMessageModel) -> EventEnvelope:
        return EventEnvelope(
            cursor=encode_event_cursor(
                created_at=message.created_at,
                event_id=message.id,
            ),
            space_id=thread_id,
            event=EventSummary(
                id=message.id,
                kind=message.event_kind,
                actor_name=message.actor_name,
                content=message.content,
                created_at=message.created_at,
            ),
        )

    def _publish(self, envelope: EventEnvelope | None) -> None:
        if envelope is None or self._broker is None:
            return
        self._broker.publish(space_id=envelope.space_id, item=envelope)
